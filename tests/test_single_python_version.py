"""All testing uses the shipped Python, and CI tests run only by dispatch."""
from __future__ import annotations

import ast
import re
import shlex
import tomllib
from itertools import product
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ("workflow_dispatch", "push", "schedule")


def _workflow():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


def _condition(expression, context):
    """Evaluate the workflow's boolean guards without executing workflow code."""
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    for name, value in context.items():
        expression = expression.replace(name, repr(value))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression).strip()

    def evaluate(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            values = [evaluate(value) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not evaluate(node.operand)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError(f"Unsupported CI condition: {expression}")

    return evaluate(ast.parse(expression, mode="eval").body)


def test_every_python_matrix_and_run_argument_uses_312():
    matrices = []
    arguments = []
    for name, job in _workflow()["jobs"].items():
        matrix = job.get("strategy", {}).get("matrix", {})
        if "python" in matrix:
            # A literal singleton has the same value for every workflow event.
            assert matrix["python"] == ["3.12"], name
            matrices.append(name)
        for step in job.get("steps", []):
            for match in re.finditer(r"--python(?:=|\s+)(\$\{\{.*?\}\}|[^\s;]+)", step.get("run", "")):
                argument = match[1]
                if argument == "${{ matrix.python }}":
                    assert matrix.get("python") == ["3.12"], name
                    argument = matrix["python"][0]
                assert shlex.split(argument) == ["3.12"], (name, argument)
                arguments.append(name)
    assert matrices, "No Python matrices checked"
    assert arguments, "No --python arguments checked"


@pytest.mark.parametrize("event", EVENTS)
def test_test_jobs_can_start_only_on_dispatch(event):
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))  # PyYAML reads 'on' as True.
    jobs = workflow["jobs"]
    test_jobs = {"collect", "shard", "test"}
    test_jobs.update(
        name for name, job in jobs.items()
        if any(re.search(r"\bpytest\b|shard_suite\.py\s+(?:collect|check)\b", step.get("run", ""))
               for step in job.get("steps", []))
    )
    for name in sorted(test_jobs):
        job = jobs[name]
        for ref, skip, cancelled, success in product(
                ("refs/heads/main", "refs/heads/candidate"), ("true", "false"), (False, True), (False, True)):
            context = {
                "github.event_name": event,
                "github.ref": ref,
                "needs.reuse.outputs.skip": skip,
                "always()": True,
                "cancelled()": cancelled,
                "success()": success,
            }
            starts = event in triggers and _condition(job.get("if", "success()"), context)
            if event != "workflow_dispatch":
                assert not starts, (name, context)
            elif not cancelled and success:
                assert starts, (name, context)


def test_every_project_requires_only_python_312():
    projects = sorted(path for path in ROOT.rglob("pyproject.toml")
                      if not {".venv", ".git"}.intersection(path.relative_to(ROOT).parts))
    assert projects
    for path in projects:
        project = tomllib.loads(path.read_text())["project"]
        requirement = project["requires-python"]
        assert requirement == ">=3.12,<3.13", path.relative_to(ROOT)
        versions = SpecifierSet(requirement)
        assert "3.12" in versions and "3.12.99" in versions
        assert "3.13" not in versions and "3.14" not in versions
