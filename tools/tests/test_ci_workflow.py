"""Offline checks for the CI suite's scheduling, shards and final status jobs."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
PYTHONS = '${{ fromJSON(github.event_name == \'push\' && \'["3.12"]\' || \'["3.12", "3.14"]\') }}'
ELIGIBLE = "github.event_name != 'push' || github.ref == 'refs/heads/main'"


class CIWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)
        self.jobs = self.workflow["jobs"]

    def test_lock_job_is_unchanged(self):
        expected = yaml.safe_load("""
name: lock and licences
runs-on: ubuntu-24.04
steps:
  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
  - uses: astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e
  - run: uv sync --locked --all-extras --python 3.14
  - name: no Elastic, AGPL, SSPL or BUSL package
    run: uv run python tools/check_lock.py uv.lock --licenses
  - name: third-party notices are current
    run: uv run python tools/check_lock.py --check-notices
""")
        self.assertEqual(self.jobs["lock"], expected)

    def test_final_jobs_keep_the_existing_names(self):
        final = self.jobs["test"]
        self.assertEqual(set(final["needs"]), {"collect", "shard"})
        self.assertEqual(final["strategy"]["matrix"], {"python": PYTHONS})
        names = [final["name"].replace("${{ matrix.python }}", version) for version in ("3.12", "3.14")]
        self.assertEqual(names, ["tests (postgresql, Python 3.12)", "tests (postgresql, Python 3.14)"])

    def test_five_shards_are_planned_and_run_per_python(self):
        self.assertEqual(set(self.jobs), {"lock", "collect", "shard", "test"})
        self.assertEqual(self.workflow["env"]["SHARDS"], "5")
        shard = self.jobs["shard"]
        self.assertEqual(shard["needs"], "collect")
        self.assertEqual(shard["strategy"]["matrix"]["index"], [0, 1, 2, 3, 4])
        self.assertFalse(shard["strategy"]["fail-fast"])
        collect_script = "\n".join(step.get("run", "") for step in self.jobs["collect"]["steps"])
        self.assertIn("tools/shard_suite.py collect", collect_script)
        self.assertIn(' --shards "$SHARDS"', collect_script)
        self.assertIn("--durations .test_durations", collect_script)
        self.assertIn("--plan-out", collect_script)

    def test_shard_count_is_consistent_across_planning_execution_and_union(self):
        shards = int(self.workflow["env"]["SHARDS"])
        self.assertEqual(shards, 5)
        indices = self.jobs["shard"]["strategy"]["matrix"]["index"]
        self.assertEqual(len(indices), shards)
        self.assertEqual(indices, list(range(shards)))
        for name, command in (("collect", "split"), ("test", "check")):
            job = self.jobs[name]
            self.assertNotIn("SHARDS", job.get("env", {}))
            steps = [step for step in job["steps"]
                     if f"tools/shard_suite.py {command}" in step.get("run", "")]
            self.assertEqual(len(steps), 1)
            step = steps[0]
            self.assertNotIn("SHARDS", step.get("env", {}))
            script = step["run"].replace("\\\n", " ")
            commands = re.findall(
                rf"(?m)^\s*(?:uv run python|python3) tools/shard_suite\.py {command}\b[^\n]*",
                script,
            )
            self.assertEqual(len(commands), 1)
            self.assertEqual(re.findall(r"--shards(?:=|\s+)(\S+)", commands[0]), ['"$SHARDS"'])

    def test_triggers_and_python_selection(self):
        # PyYAML's YAML 1.1 loader treats the unquoted key 'on' as True.
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertEqual(triggers, {"push": None, "schedule": [{"cron": "17 6 * * 1"}],
                                    "workflow_dispatch": None})
        for name in ("collect", "shard", "test"):
            job = self.jobs[name]
            condition = job["if"]
            if condition.startswith("${{"):
                condition = condition[3:-2].strip()
            allowed = {ELIGIBLE, f"!cancelled() && ({ELIGIBLE})"}
            if name == "test":
                allowed = {f"always() && ({ELIGIBLE})"}
            self.assertIn(condition, allowed)
            self.assertEqual(job["strategy"]["matrix"]["python"], PYTHONS)
            # The condition excludes branch/tag pushes; dispatch and schedule include both versions.
            lists = re.findall(r"'([\[][\]0-9.,\" ]+)'", job["strategy"]["matrix"]["python"])
            self.assertEqual([json.loads(value) for value in lists], [["3.12"], ["3.12", "3.14"]])

    def test_final_job_runs_after_upstream_failure_and_checks_the_union(self):
        final = self.jobs["test"]
        self.assertEqual(final["if"], "${{ always() && (" + ELIGIBLE + ") }}")
        first = final["steps"][0]
        self.assertEqual(first["if"], "always()")
        self.assertFalse(first.get("continue-on-error", False))
        self.assertEqual(first["env"], {"COLLECT_RESULT": "${{ needs.collect.result }}",
                                        "SHARD_JOBS_RESULT": "${{ needs.shard.result }}"})
        self.assertIn('if [ "$COLLECT_RESULT" != success ] || [ "$SHARD_JOBS_RESULT" != success ]; then',
                      first["run"])
        self.assertIn("exit 1", first["run"])
        checks = [step for step in final["steps"] if "tools/shard_suite.py check" in step.get("run", "")]
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0].get("continue-on-error", False))
        for option in ("--collected", "--plan", '--shards "$SHARDS"', "--junit-dir", "--summary", "--report"):
            self.assertIn(option, checks[0]["run"])
        status = [step for step in final["steps"] if "SHARD_RESULT" in step.get("env", {})]
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["if"], "always()")
        self.assertEqual(status[0]["env"]["SHARD_RESULT"], "${{ needs.shard.result }}")
        self.assertIn('[ "$SHARD_RESULT" = success ]', status[0]["run"])
        self.assertFalse(status[0].get("continue-on-error", False))

    def test_artifacts_are_isolated_by_python_and_reports_upload_on_failure(self):
        for name in ("collect", "shard", "test"):
            for step in self.jobs[name]["steps"]:
                if "artifact@" not in step.get("uses", ""):
                    continue
                artifact = step["with"].get("name", step["with"].get("pattern", ""))
                self.assertIn("${{ matrix.python }}", artifact)
                if name == "shard" and step["uses"].startswith("actions/upload-artifact@"):
                    self.assertIn("${{ matrix.index }}", artifact)
                    self.assertEqual(step["if"], "always()")

    def test_collect_and_shard_have_the_existing_service_and_setup(self):
        for name in ("collect", "shard"):
            job = self.jobs[name]
            service = job["services"]["postgres"]
            self.assertEqual(service["image"], "postgres:18.6-trixie@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6")
            self.assertEqual(service["env"], {"POSTGRES_USER": "erpclaw", "POSTGRES_PASSWORD": "ci-only",
                                              "POSTGRES_DB": "erpclaw_test"})
            self.assertEqual(service["ports"], ["5432:5432"])
            script = "\n".join(step.get("run", "") for step in job["steps"])
            self.assertIn("uv sync --locked --all-extras --python ${{ matrix.python }}", script)
            self.assertIn("551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  /tmp/gitleaks.tgz", script)
            self.assertIn("sha256sum -c -", script)
            self.assertIn('echo "ERPCLAW_GITLEAKS=/tmp/gitleaks" >> "$GITHUB_ENV"', script)
            commands = [step for step in job["steps"] if ("pytest -q" if name == "shard" else "shard_suite.py collect") in step.get("run", "")]
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]["env"]["ERPCLAW_PG_TEST_URL"],
                             "postgresql+psycopg://erpclaw:ci-only@127.0.0.1:5432/erpclaw_test")
            if name == "shard":
                self.assertIn('(( ${#files[@]} > 0 ))', commands[0]["run"])
                self.assertIn('"${files[@]}"', commands[0]["run"])
                self.assertIn("--junitxml=", commands[0]["run"])

    def test_actions_are_pinned_and_permissions_are_read_only(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", self.text)
        actions = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", self.text, re.MULTILINE)
        self.assertTrue(actions)
        for action in actions:
            self.assertRegex(action, r"^[\w./-]+@[0-9a-f]{40}$")
            self.assertIn(action.split("@")[0], {"actions/checkout", "astral-sh/setup-uv",
                                                 "actions/upload-artifact", "actions/download-artifact"})
        for job in self.jobs.values():
            self.assertNotIn("permissions", job)
            self.assertNotIn("secrets", job)

    def test_separate_sharded_workflow_is_removed(self):
        self.assertFalse((ROOT / ".github/workflows/suite-sharded.yml").exists())


if __name__ == "__main__":
    unittest.main()


class CIFinalJobCancellationTests(unittest.TestCase):
    def setUp(self):
        self.final = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["test"]

    def test_final_job_runs_even_when_workflow_is_cancelled(self):
        self.assertEqual(self.final["if"], "${{ always() && (" + ELIGIBLE + ") }}")
        self.assertNotIn("!cancelled()", self.final["if"])

    def test_first_step_requires_successful_collection_and_shards(self):
        first = self.final["steps"][0]
        self.assertIn("run", first, "The prerequisite check must precede checkout and downloads")
        self.assertEqual(first.get("if"), "always()")
        self.assertFalse(first.get("continue-on-error", False))
        self.assertIn("${{ needs.collect.result }}", first.get("env", {}).values())
        self.assertIn("${{ needs.shard.result }}", first.get("env", {}).values())
        self.assertIn("success", first["run"])
        self.assertRegex(first["run"], r"\bexit\s+[1-9][0-9]*\b")

    def test_incomplete_prerequisites_cannot_skip_or_pass_the_final_job(self):
        import subprocess

        # A status function prevents GitHub's implicit success() dependency gate.
        self.assertEqual(set(self.final["needs"]), {"collect", "shard"})
        self.assertTrue(self.final["if"].startswith("${{ always() && ("))
        self.assertFalse(self.final.get("continue-on-error", False))
        self.assertFalse(self.final["strategy"]["fail-fast"])
        first = self.final["steps"][0]
        self.assertIn("run", first)
        self.assertFalse(first.get("continue-on-error", False))
        statuses = ("success", "failure", "cancelled", "skipped", "")
        for collect in statuses:
            for shard in statuses:
                with self.subTest(collect=collect, shard=shard):
                    results = {"${{ needs.collect.result }}": collect,
                               "${{ needs.shard.result }}": shard}
                    env = {name: results.get(value, value) for name, value in first.get("env", {}).items()}
                    result = subprocess.run(
                        ["bash", "-e", "-o", "pipefail", "-c", first["run"]],
                        env=env, capture_output=True, text=True, timeout=5,
                    )
                    if collect == shard == "success":
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertTrue(result.stdout.strip(), "Explain the prerequisite failure")
