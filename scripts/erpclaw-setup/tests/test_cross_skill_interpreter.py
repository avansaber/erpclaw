"""Child skill actions run under the caller's interpreter.

A cross-skill child must be launched with ``sys.executable`` (the
interpreter running the caller), never the ``python3`` found on PATH,
so a virtual-environment install keeps its drivers in the child.
"""
import argparse
import ast
import importlib.util
import json
import os
import sys

import pytest

SETUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Bind erpclaw_lib to the tree under test, never the deployed
# ~/.openclaw/erpclaw/lib symlink (same approach as test_cross_skill_argv.py).
_IN_TREE_LIB = os.path.join(SETUP_DIR, "lib")
ERPCLAW_LIB = (_IN_TREE_LIB if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib"))
               else os.path.join(os.path.expanduser(
                   os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
if ERPCLAW_LIB not in sys.path:
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, ERPCLAW_LIB)

from erpclaw_lib import cross_skill  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))))
SOURCE_DIR = os.path.join(ROOT_DIR, "source")


class _Completed:
    def __init__(self, payload):
        self.stdout = json.dumps(payload)
        self.stderr = ""
        self.returncode = 0


def _capture_run(monkeypatch, payload):
    """Patch out resolve + subprocess; return the captured argv list."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Completed(payload)

    monkeypatch.setattr(cross_skill, "resolve_skill_script",
                        lambda skill: "/tmp/fake/db_query.py")
    monkeypatch.setattr(cross_skill.subprocess, "run", _fake_run)
    return calls


def test_child_uses_the_caller_interpreter(monkeypatch):
    calls = _capture_run(monkeypatch, {"status": "ok"})
    cross_skill.call_skill_action("erpclaw", "list-items", {})
    assert len(calls) == 1
    assert calls[0][0] == sys.executable
    assert calls[0][1:] == ["/tmp/fake/db_query.py", "--action", "list-items"]


def test_child_falls_back_when_executable_unknown(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    assert cross_skill.child_interpreter() == "python3"
    calls = _capture_run(monkeypatch, {"status": "ok"})
    cross_skill.call_skill_action("erpclaw", "list-items", {})
    assert len(calls) == 1
    assert calls[0][0] == "python3"


class _StubCursor:
    def fetchone(self):
        return ("row",)


class _StubConn:
    """Minimal stand-in: the helper only probes for presence, then exits."""

    def __init__(self):
        self.company_id = "CO-1"

    def execute(self, *args, **kwargs):
        return _StubCursor()


def _load_crm_module():
    path = os.path.join(SOURCE_DIR, "erpclaw-addons", "erpclaw-growth",
                        "scripts", "erpclaw-crm", "db_query.py")
    spec = importlib.util.spec_from_file_location("crm_db_query_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_crm_list_customers_child_uses_the_caller_interpreter(monkeypatch):
    crm = _load_crm_module()
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Completed({"status": "ok", "customers": [{"id": "C1"}]})

    monkeypatch.setattr("erpclaw_lib.dependencies.resolve_skill_script",
                        lambda skill: "/tmp/fake-selling/db_query.py")
    monkeypatch.setattr(crm.subprocess, "run", _fake_run)
    args = argparse.Namespace(limit=20, offset=0, db_path=None)
    with pytest.raises(SystemExit) as exc:
        crm._apply_saved_view_customer(_StubConn(), args, {"filter_json": None})
    assert exc.value.code == 0
    assert len(calls) == 1
    assert calls[0][0] == sys.executable
    assert calls[0][calls[0].index("--action") + 1] == "list-customers"


# Non-launch "python3" occurrences: path + line + one-line reason each.
_NON_LAUNCH_EXCEPTIONS = {
    ("source/erpclaw-addons/erpclaw-os-engine/scripts/generate_module.py", 861):
        "template text for generated skill metadata, never spawned as a process",
    ("source/erpclaw-addons/erpclaw-os-engine/scripts/web_dashboard.py", 128):
        "creates an isolated venv via python3 -m venv, not a child action",
}


def _child_launch_hits(path):
    """Line numbers starting a list argv with the literal "python3"."""
    with open(path) as handle:
        tree = ast.parse(handle.read())
    hits = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.funcs = []

        def visit_FunctionDef(self, node):
            self.funcs.append(node.name)
            self.generic_visit(node)
            self.funcs.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def _check(self, node):
            if (node.elts and isinstance(node.elts[0], ast.Constant)
                    and node.elts[0].value == "python3"):
                hits.append((node.lineno,
                             self.funcs[-1] if self.funcs else None))
            self.generic_visit(node)

        visit_List = _check
        visit_Tuple = _check

    _Visitor().visit(tree)
    return hits


def test_no_python3_literal_in_child_launches():
    for rel, line in _NON_LAUNCH_EXCEPTIONS:
        with open(os.path.join(ROOT_DIR, rel)) as handle:
            numbered = handle.read().splitlines()
        assert "python3" in numbered[line - 1], f"stale exception: {rel}:{line}"
    violations = []
    for base, _dirs, files in os.walk(SOURCE_DIR):
        if "tests" in base.split(os.sep):
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            if name.startswith("test_") or name.endswith("_test.py"):
                continue
            if name == "conftest.py":
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT_DIR)
            for lineno, func in _child_launch_hits(path):
                if (os.path.basename(path) == "cross_skill.py"
                        and func == "child_interpreter"):
                    continue
                if (rel, lineno) in _NON_LAUNCH_EXCEPTIONS:
                    continue
                violations.append(f"{rel}:{lineno}")
    assert not violations, f"child launches on bare python3: {violations}"
