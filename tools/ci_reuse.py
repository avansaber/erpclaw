#!/usr/bin/env python3
"""Decide whether a push can reuse a dispatched CI run, using saved REST responses.

Both inputs accept one API response object or an array of paginated response
objects (gh api --paginate --slurp). Jobs may cover several runs; run_id keeps
their results separate. Missing, malformed or error responses mean run the suite.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_JOBS = (
    "tests (postgresql, Python 3.12)",
    "tests (postgresql, Python 3.14)",
)


def read_items(path: Path, key: str) -> list[dict]:
    response = json.loads(path.read_text(encoding="utf-8"))
    pages = response if isinstance(response, list) else [response]
    items = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get(key), list):
            raise ValueError(f"invalid {key} API response")
        if any(not isinstance(item, dict) for item in page[key]):
            raise ValueError(f"invalid {key} entries")
        items.extend(page[key])
    return items


def decide(runs_path: Path, jobs_path: Path, sha: str) -> tuple[bool, str]:
    try:
        runs = read_items(runs_path, "workflow_runs")
        jobs = read_items(jobs_path, "jobs")
    except (OSError, ValueError, UnicodeError):
        return False, "unavailable or malformed API response; run the suite"

    for run in runs:
        run_id = run.get("id")
        if (type(run_id) is not int or run_id <= 0
                or run.get("head_sha") != sha
                or run.get("event") != "workflow_dispatch"
                or run.get("status") != "completed"
                or run.get("conclusion") != "success"):
            continue
        required = [job for job in jobs
                    if job.get("run_id") == run_id and job.get("name") in REQUIRED_JOBS]
        if ({job["name"] for job in required} == set(REQUIRED_JOBS)
                and all(job.get("status") == "completed" and job.get("conclusion") == "success"
                        for job in required)):
            return True, f"dispatched run {run_id} succeeded with both required Python test jobs"
    return False, "no successful dispatched run for this commit with both Python test jobs; run the suite"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    skip, reason = decide(args.runs, args.jobs, args.sha)
    # Both lines are also valid GITHUB_OUTPUT entries; reasons contain no API text.
    print(f"skip={str(skip).lower()}")
    print(f"reason={reason}")


if __name__ == "__main__":
    main()
