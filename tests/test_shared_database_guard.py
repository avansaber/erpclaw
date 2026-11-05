"""The test database is shared by every lane of the workspace: the suite opens it only while the workspace's suite
lock is held, in CI, or when the run's database is its own (a review clone's)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _import_conftest(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", "import conftest"], cwd=TESTS, env=env, capture_output=True,
                          text=True)


def _base() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ERPCLAW_LOCKS_HELD", "GITHUB_ACTIONS", "ERPCLAW_TEST_DB_PRIVATE")}
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


def test_the_suite_refuses_the_shared_database_without_the_suite_lock():
    proc = _import_conftest(_base())
    assert proc.returncode != 0 and "suite lock" in proc.stderr, proc.stderr[-400:]


def test_the_suite_opens_it_with_the_suite_lock_held(tmp_path):
    env = {**_base(), "ERPCLAW_LOCKS_HELD": str(tmp_path / ".lock-model") + os.pathsep + str(tmp_path / ".lock-suite")}
    assert _import_conftest(env).returncode == 0


def test_another_lock_alone_is_not_the_suite_lock(tmp_path):
    env = {**_base(), "ERPCLAW_LOCKS_HELD": str(tmp_path / ".lock-model")}
    assert _import_conftest(env).returncode != 0


def test_ci_and_a_private_database_open_it_too():
    assert _import_conftest({**_base(), "GITHUB_ACTIONS": "true"}).returncode == 0
    assert _import_conftest({**_base(), "ERPCLAW_TEST_DB_PRIVATE": "1"}).returncode == 0
