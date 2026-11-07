"""Commit-message plan rules for each scanner mode."""
from __future__ import annotations

import pytest

from test_push_scan import commit, git, scan, setup  # noqa: F401


TASK = f"M{9}-T{7}"
PUBLIC_FINDING = "plan id in a public commit message"


def public_findings(out: str) -> list[str]:
    return [line for line in out.splitlines() if PUBLIC_FINDING in line]


@pytest.mark.parametrize("trailer", [
    "Plan: " + TASK,
    "Plan: confidential-value",
    "Plan:",
    "pLaN: confidential-value",
], ids=["task-value", "arbitrary-value", "empty-value", "mixed-case-key"])
def test_public_trailer_blocks_with_redacted_location(setup, trailer):
    make, policy = setup
    repo = make("other-repo")
    commit(repo, {"example.py": "value = 1\n"}, "Release\n\n" + trailer)
    sha = git(repo, "rev-parse", "HEAD").strip()
    code, out = scan(repo, policy)
    assert "Mode: public" in out, out
    assert code == 1, out
    findings = public_findings(out)
    assert findings, out
    assert all("HARD BLOCK" in line and f"commit={sha[:12]}" in line
               and 'path="<commit-message>"' in line and "value=<redacted len=" in line
               for line in findings), out
    assert TASK not in out and "confidential-value" not in out, out
    assert "Release" not in out and "no Plan trailer" not in out, out


@pytest.mark.parametrize("message,value,line_no", [
    ("Release " + TASK, TASK, 1),
    ("Release\n\nprivate-prefix " + TASK + " private-suffix", TASK, 3),
    ("Release\n\nprivate-prefix " + TASK + "a private-suffix", TASK + "a", 3),
    ("Release\n\nPlan: " + TASK + "\n\nMore body after the line.", TASK, 3),
], ids=["subject", "body", "suffix", "body-plan-line"])
def test_public_task_id_anywhere_blocks_with_only_match_length(setup, message, value, line_no):
    make, policy = setup
    repo = make("other-repo")
    commit(repo, {"example.py": "value = 1\n"}, message)
    sha = git(repo, "rev-parse", "HEAD").strip()
    code, out = scan(repo, policy)
    assert code == 1, out
    assert public_findings(out) == [
        f'  HARD BLOCK: commit={sha[:12]} path="<commit-message>" line={line_no} '
        f"label='{PUBLIC_FINDING}' value=<redacted len={len(value)}>"
    ], out
    assert value not in out and "private-prefix" not in out and "private-suffix" not in out, out
    assert "no Plan trailer" not in out, out


@pytest.mark.parametrize("message", [
    "Release\n\nA public description.",
    "Release\n\nPlan: a body line\n\nMore body after the line.",
    "Release\n\nLonger identifier " + TASK + "abc",
], ids=["plain", "body-plan-line", "word-boundary"])
def test_public_without_plan_ids_passes(setup, message):
    make, policy = setup
    repo = make("other-repo")
    commit(repo, {"example.py": "value = 1\n"}, message)
    code, out = scan(repo, policy)
    assert "Mode: public" in out, out
    assert code == 0, out
    assert not public_findings(out) and "no Plan trailer" not in out, out


@pytest.mark.parametrize("trailer", [False, True])
def test_clean_still_requires_trailer(setup, trailer):
    make, policy = setup
    repo = make("engine-repo")
    message = "Release" + ("\n\nPlan: " + TASK if trailer else "")
    commit(repo, {"example.py": "value = 1\n"}, message)
    code, out = scan(repo, policy)
    assert "Mode: clean" in out, out
    assert code == (0 if trailer else 1), out
    assert ("no Plan trailer" in out) is not trailer, out
    assert not public_findings(out), out


@pytest.mark.parametrize("trailer", [False, True])
def test_private_allows_task_ids_with_or_without_trailer(setup, trailer):
    make, policy = setup
    repo = make("notes-repo")
    message = "Release\n\nBody mentions " + TASK
    if trailer:
        message += "\n\nPlan: " + TASK
    commit(repo, {"example.py": "value = 1\n"}, message)
    code, out = scan(repo, policy)
    assert "Mode: private" in out, out
    assert code == 0, out
    assert not public_findings(out) and "no Plan trailer" not in out, out


def test_public_range_names_only_commit_with_trailer(setup):
    make, policy = setup
    repo = make("other-repo")
    commit(repo, {"example.py": "value = 1\n"}, "Release")
    first = git(repo, "rev-parse", "HEAD").strip()
    commit(repo, {"example.py": "value = 2\n"}, "Update\n\nPlan: " + TASK)
    second = git(repo, "rev-parse", "HEAD").strip()
    code, out = scan(repo, policy)
    assert code == 1 and "Commits introduced: 2" in out, out
    findings = public_findings(out)
    assert findings and all(f"commit={second[:12]}" in line for line in findings), out
    assert all('path="<commit-message>"' in line for line in findings), out
    assert not any(f"commit={first[:12]}" in line for line in out.splitlines()
                   if "HARD BLOCK" in line), out
    assert TASK not in out and "no Plan trailer" not in out, out
