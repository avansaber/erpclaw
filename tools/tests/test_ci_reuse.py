"""Offline REST fixtures exercise the actual reuse decision command."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "ci_reuse.py"
SHA = "a" * 40
NAMES = [f"tests (postgresql, Python {version})" for version in ("3.12", "3.14")]


@pytest.fixture
def runs():
    return {"workflow_runs": [{"id": 42, "event": "workflow_dispatch", "head_sha": SHA,
                               "status": "completed", "conclusion": "success"}]}


@pytest.fixture
def jobs():
    return {"jobs": [{"run_id": 42, "name": name, "status": "completed", "conclusion": "success"}
                     for name in NAMES]}


def invoke(tmp_path, runs, jobs, *, skip):
    runs_file, jobs_file = tmp_path / "runs.json", tmp_path / "jobs.json"
    for path, data in ((runs_file, runs), (jobs_file, jobs)):
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--runs", str(runs_file), "--jobs", str(jobs_file), "--sha", SHA],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == f"skip={str(skip).lower()}"
    assert len(lines) == 2 and lines[1].startswith("reason=") and len(lines[1]) > len("reason=")
    return result.stdout


def test_both_pythons_green_prints_the_reused_run_id(tmp_path, runs, jobs):
    assert "run 42" in invoke(tmp_path, runs, jobs, skip=True)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
def test_one_python_not_successful(tmp_path, runs, jobs, conclusion):
    jobs["jobs"][1]["conclusion"] = conclusion
    invoke(tmp_path, runs, jobs, skip=False)


@pytest.mark.parametrize("missing", [0, 1])
def test_one_python_missing(tmp_path, runs, jobs, missing):
    jobs["jobs"].pop(missing)
    invoke(tmp_path, runs, jobs, skip=False)


@pytest.mark.parametrize("changes", [
    {"status": "in_progress", "conclusion": None},
    {"status": "queued", "conclusion": None},
    {"status": "in_progress"},
    {"head_sha": "b" * 40},
    {"conclusion": "cancelled"},
    {"conclusion": "failure"},
    {"event": "push"},
    {"event": "schedule"},
    {"id": None},
])
def test_ineligible_run_even_with_green_jobs(tmp_path, runs, jobs, changes):
    runs["workflow_runs"][0].update(changes)
    invoke(tmp_path, runs, jobs, skip=False)


def test_running_job_is_not_reused(tmp_path, runs, jobs):
    jobs["jobs"][1]["status"] = "in_progress"
    invoke(tmp_path, runs, jobs, skip=False)


@pytest.mark.parametrize("empty", [{"workflow_runs": []}, []])
def test_empty_run_list(tmp_path, jobs, empty):
    invoke(tmp_path, empty, jobs, skip=False)


@pytest.mark.parametrize("bad", ["{invalid json", "", "null", {}, {"message": "API error"},
                                 {"workflow_runs": None}, {"workflow_runs": [None]}])
def test_bad_runs_response_falls_back(tmp_path, jobs, bad):
    invoke(tmp_path, bad, jobs, skip=False)


@pytest.mark.parametrize("bad", ["{invalid json", "", "null", {}, {"message": "API error"},
                                 {"jobs": None}, {"jobs": [None]}, {"jobs": []}, []])
def test_bad_or_empty_jobs_response_falls_back(tmp_path, runs, bad):
    invoke(tmp_path, runs, bad, skip=False)


def test_missing_response_files_fall_back(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--runs", str(tmp_path / "absent-runs.json"),
         "--jobs", str(tmp_path / "absent-jobs.json"), "--sha", SHA],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("skip=false\nreason=")


def test_cannot_combine_pythons_from_different_runs(tmp_path, runs, jobs):
    runs["workflow_runs"].append(dict(runs["workflow_runs"][0], id=43))
    jobs["jobs"][1]["run_id"] = 43
    invoke(tmp_path, runs, jobs, skip=False)


def test_exact_job_names_are_required(tmp_path, runs, jobs):
    jobs["jobs"][1]["name"] = "shard 0 (postgresql, Python 3.14)"
    invoke(tmp_path, runs, jobs, skip=False)


def test_any_eligible_run_across_pages_can_be_reused(tmp_path, runs, jobs):
    other = dict(runs["workflow_runs"][0], id=43)
    runs = [{"workflow_runs": [other]}, runs]
    jobs = [{"jobs": [dict(jobs["jobs"][0], run_id=43, conclusion="failure")]},
            {"jobs": [jobs["jobs"][0]]}, {"jobs": [jobs["jobs"][1]]}]
    assert "run 42" in invoke(tmp_path, runs, jobs, skip=True)


def test_error_page_prevents_reuse_of_partial_results(tmp_path, runs, jobs):
    invoke(tmp_path, runs, [jobs, {"message": "API error"}], skip=False)
