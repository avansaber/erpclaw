"""Offline checks for the CI suite's scheduling, shards and final status jobs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
PYTHONS = ["3.12"]
DISPATCH_ONLY = "github.event_name == 'workflow_dispatch'"
ELIGIBLE = "github.event_name != 'push' || github.ref == 'refs/heads/main'"
REUSE_ALLOWED = "github.event_name != 'push' || needs.reuse.outputs.skip != 'true'"
GITLEAKS_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"


class CIWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)
        self.jobs = self.workflow["jobs"]

    def test_lock_job_rejects_missing_or_changed_release_version_step(self):
        expected = yaml.safe_load("""
name: lock and licences
runs-on: [self-hosted, erpclaw-ci]
steps:
  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
  - uses: astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e
  - run: uv sync --locked --all-extras --python 3.12
  - name: check plugin release versions
    run: uv run python tools/release_rule.py
  - name: no Elastic, AGPL, SSPL or BUSL package
    run: uv run python tools/check_lock.py uv.lock --licenses
  - name: third-party notices are current
    run: uv run python tools/check_lock.py --check-notices
""")
        self.assertEqual(self.jobs["lock"], expected)

    def test_final_jobs_keep_the_existing_names(self):
        final = self.jobs["test"]
        self.assertEqual(set(final["needs"]), {"reuse", "collect", "shard"})
        self.assertEqual(final["runs-on"], ["self-hosted", "light"])
        self.assertEqual(final["strategy"]["matrix"], {"python": PYTHONS})
        names = [final["name"].replace("${{ matrix.python }}", version) for version in PYTHONS]
        self.assertEqual(names, ["tests (postgresql, Python 3.12)"])

    def test_pytest_and_postgresql_jobs_do_not_use_light_runner(self):
        for name, job in self.jobs.items():
            commands = "\n".join(step.get("run", "") for step in job.get("steps", []))
            runs_pytest = re.search(r"\bpytest\b|tools/shard_suite\.py\s+collect\b", commands)
            runs_postgres = any(
                "postgres" in service_name.lower() or "postgres" in service.get("image", "").lower()
                for service_name, service in job.get("services", {}).items()
            )
            if runs_pytest or runs_postgres:
                with self.subTest(job=name):
                    self.assertNotIn("light", job["runs-on"])

    def test_ten_shards_are_planned_and_run_per_python(self):
        self.assertEqual(set(self.jobs), {"reuse", "seed-gitleaks-cache", "lock", "collect", "shard", "test"})
        self.assertEqual(self.workflow["env"]["SHARDS"], "10")
        shard = self.jobs["shard"]
        self.assertEqual(set(shard["needs"]), {"reuse", "collect"})
        self.assertEqual(shard["strategy"]["matrix"]["index"], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertFalse(shard["strategy"]["fail-fast"])
        collect_script = "\n".join(step.get("run", "") for step in self.jobs["collect"]["steps"])
        self.assertIn("tools/shard_suite.py collect", collect_script)
        self.assertIn(' --shards "$SHARDS"', collect_script)
        self.assertIn("--durations .test_durations", collect_script)
        self.assertIn("--plan-out", collect_script)

    def test_shard_count_is_consistent_across_planning_execution_and_union(self):
        shards = int(self.workflow["env"]["SHARDS"])
        self.assertEqual(shards, 10)
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
        self.assertEqual(triggers, {"push": {"branches": ["main"]}, "schedule": [{"cron": "17 6 * * 1"}],
                                    "workflow_dispatch": None})
        for name in ("collect", "shard", "test"):
            job = self.jobs[name]
            self.assertEqual(job["timeout-minutes"], 180)
            condition = job["if"]
            if condition.startswith("${{"):
                condition = condition[3:-2].strip()
            status = "always()" if name == "test" else "!cancelled()"
            self.assertEqual(condition, f"{status} && {DISPATCH_ONLY} && ({ELIGIBLE}) && ({REUSE_ALLOWED})")
            self.assertEqual(job["strategy"]["matrix"]["python"], PYTHONS)
        # Every Python matrix is a literal singleton, independent of the event.
        for job in self.jobs.values():
            matrix = job.get("strategy", {}).get("matrix", {})
            if "python" in matrix:
                self.assertEqual(matrix["python"], ["3.12"])

    def test_final_job_runs_after_upstream_failure_and_checks_the_union(self):
        final = self.jobs["test"]
        self.assertEqual(final["if"], "${{ always() && " + DISPATCH_ONLY + " && (" + ELIGIBLE + ") && (" + REUSE_ALLOWED + ") }}")
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

    def test_shards_record_non_strict_xpasses_in_the_junit_report(self):
        step = next(step for step in self.jobs["shard"]["steps"] if "pytest -q" in step.get("run", ""))
        self.assertIn("-p shard_suite", step["run"])
        self.assertEqual(step["env"]["PYTHONPATH"], "${{ github.workspace }}/tools")

    def test_final_summary_uses_report_outcomes_and_rejects_only_strict_xpasses(self):
        step = next(step for step in self.jobs["test"]["steps"]
                    if "tools/shard_suite.py check" in step.get("run", ""))
        for xpass in (None, "strict", "non-strict"):
            with self.subTest(xpass=xpass), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                plan_dir, junit = temp / "shard-plan", temp / "junit"
                plan_dir.mkdir()
                junit.mkdir()
                ids = [f"tests/test_outcomes.py::test_case[{n}]" for n in range(25)]
                (plan_dir / "nodeids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8")
                digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()
                plan = {"format": 1, "shards": 5, "collected_sha256": digest,
                        "plan": [{"index": i, "nodeids": ids[i * 5:(i + 1) * 5]} for i in range(5)]}
                (plan_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
                for shard in plan["plan"]:
                    suite = ET.Element("testsuite", name="pytest")
                    for nodeid in shard["nodeids"]:
                        number = ids.index(nodeid)
                        case = ET.SubElement(suite, "testcase", classname="tests.test_outcomes",
                                             name=nodeid.split("::", 1)[1])
                        if number < 18:
                            ET.SubElement(case, "skipped", type="pytest.skip")
                        elif number < 22:
                            ET.SubElement(case, "skipped", type="pytest.xfail")
                        elif number == 22 and xpass == "strict":
                            ET.SubElement(case, "failure", message="[XPASS(strict)] fixed")
                        elif number == 22 and xpass == "non-strict":
                            properties = ET.SubElement(case, "properties")
                            ET.SubElement(properties, "property", name="pytest.xpass", value="true")
                    ET.ElementTree(suite).write(junit / f"shard-{shard['index']}.xml", encoding="utf-8")
                env = {**os.environ, "RUNNER_TEMP": str(temp), "SHARDS": "5", "SHA": "a" * 40,
                       "GITHUB_STEP_SUMMARY": str(temp / "summary.md"),
                       "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, int(xpass == "strict"), result.stdout + result.stderr)
                self.assertIn(f"18 skipped, 4 xfailed, {int(xpass is not None)} xpassed", result.stdout)
                self.assertNotIn("22 skipped", result.stdout)
                if xpass == "strict":
                    self.assertIn("strict xpass: 1", result.stdout)
                    self.assertIn(ids[22], result.stdout)
                totals = json.loads((temp / "union/report.json").read_text())["totals"]
                self.assertEqual(totals["skipped"], 18)
                self.assertEqual(totals["xfailed"], 4)
                self.assertEqual(totals["xpassed"], int(xpass is not None))

    def test_collect_and_shard_have_the_existing_service_and_setup(self):
        for name in ("collect", "shard"):
            job = self.jobs[name]
            service = job["services"]["postgres"]
            self.assertEqual(service["image"], "postgres:18.6-trixie@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6")
            self.assertEqual(service["env"], {"POSTGRES_USER": "erpclaw", "POSTGRES_PASSWORD": "ci-only",
                                              "POSTGRES_DB": "erpclaw_test"})
            port = "${{ job.services.postgres.ports['5432'] }}"
            self.assertEqual(service["ports"], ["5432"])
            script = "\n".join(step.get("run", "") for step in job["steps"])
            self.assertIn("uv sync --locked --all-extras --python ${{ matrix.python }}", script)
            self.assertIn("551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  $RUNNER_TEMP/gitleaks.tgz", script)
            self.assertIn("sha256sum -c -", script)
            self.assertIn('echo "ERPCLAW_GITLEAKS=$RUNNER_TEMP/gitleaks" >> "$GITHUB_ENV"', script)
            commands = [step for step in job["steps"] if ("pytest -q" if name == "shard" else "shard_suite.py collect") in step.get("run", "")]
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]["env"]["ERPCLAW_PG_TEST_URL"],
                             f"postgresql+psycopg://erpclaw:ci-only@127.0.0.1:{port}/erpclaw_test")
            if name == "shard":
                self.assertIn('(( ${#files[@]} > 0 ))', commands[0]["run"])
                self.assertIn('"${files[@]}"', commands[0]["run"])
                self.assertIn("--junitxml=", commands[0]["run"])

    def test_seed_gitleaks_cache_is_independent_and_runs_only_on_main_push_schedule_or_dispatch(self):
        seed = self.jobs["seed-gitleaks-cache"]
        self.assertEqual({key: value for key, value in seed.items() if key != "steps"}, {
            "name": "seed gitleaks cache",
            "if": "${{ (github.event_name == 'push' && github.ref == 'refs/heads/main') || "
                  "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' }}",
            "runs-on": ["self-hosted", "erpclaw-ci"],
            "timeout-minutes": 5,
            "permissions": {"contents": "read"},
        })
        for name in ("collect", "shard"):
            self.assertEqual(seed["steps"], self.jobs[name]["steps"][3:7])
        self.assertNotIn("pytest", json.dumps(seed))

    def test_existing_jobs_including_conditions_and_steps_are_unchanged(self):
        # Fingerprints include the shard's JUnit xpass hook and isolated gitleaks folders.
        # Ignore YAML formatting, but pin every existing condition, dependency and step.
        expected = {
            "reuse": "17c7906a7661dfeaec0de92d2978bf28c8428aafdb12ea90dd22e8f6aa131669",
            "lock": "dac11d85334a2c9a319414f243bf19d0db00dd1e6f14455408b5ca446b1e9806",
            "collect": "6d49b1c3d4b854c6de5eded3a05c491afee7a6004e543312bc863662099921a1",
            "shard": "cdee484c6421e163faa1ffab228597738609ce9ea562e289f51ccc64f9c52009",
            "test": "f1f8222dec38cac0cc1c41fc4d91a8a6df8c5b222d5556a006873a9dbfabf3e9",
        }
        for name, digest in expected.items():
            with self.subTest(job=name):
                actual = hashlib.sha256(json.dumps(self.jobs[name], sort_keys=True).encode()).hexdigest()
                self.assertEqual(actual, digest)

    def test_gitleaks_cache_is_verified_before_saving_or_installing(self):
        keys = []
        for name in ("collect", "shard", "seed-gitleaks-cache"):
            with self.subTest(job=name):
                steps = self.jobs[name]["steps"]
                restores = [step for step in steps
                            if step.get("uses", "").startswith("actions/cache/restore@")]
                saves = [step for step in steps
                         if step.get("uses", "").startswith("actions/cache/save@")]
                downloads = [step for step in steps if "curl " in step.get("run", "")]
                checks = [step for step in steps if "sha256sum" in step.get("run", "")]
                installs = [step for step in steps if 'tar -xzf "$RUNNER_TEMP/gitleaks.tgz"' in step.get("run", "")]
                for group in (restores, saves, downloads, checks):
                    self.assertEqual(len(group), 1)
                restore, save, download, check = (
                    restores[0], saves[0], downloads[0], checks[0]
                )
                self.assertEqual(restore["id"], "gitleaks-cache")
                self.assertIn(GITLEAKS_SHA256, restore["with"]["key"])
                self.assertEqual(restore["with"], save["with"])
                self.assertEqual(restore["with"]["path"], "${{ runner.temp }}/gitleaks.tgz")
                self.assertNotIn("restore-keys", restore["with"])
                keys.append(restore["with"]["key"])
                miss = "steps.gitleaks-cache.outputs.cache-hit != 'true'"
                self.assertEqual(download["if"], miss)
                self.assertEqual(save["if"], miss)
                for step in (restore, check, *installs):
                    self.assertNotIn("if", step)
                for step in (restore, download, check, save, *installs):
                    self.assertFalse(step.get("continue-on-error", False))
                self.assertLess(steps.index(restore), steps.index(download))
                self.assertLess(steps.index(restore), steps.index(check))
                self.assertLess(steps.index(download), steps.index(check))
                self.assertLess(steps.index(check), steps.index(save))
                self.assertEqual(len(installs), 0 if name == "seed-gitleaks-cache" else 1)
                for install in installs:
                    self.assertLess(steps.index(save), steps.index(install))
                self.assertEqual(check["run"].strip(),
                                 f'echo "{GITLEAKS_SHA256}  $RUNNER_TEMP/gitleaks.tgz" | sha256sum -c -')
                self.assertIn('curl -fsSLo "$RUNNER_TEMP/gitleaks.tgz"', download["run"])
                self.assertIn("/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz", download["run"])
        self.assertEqual(len(set(keys)), 1)

    def test_gitleaks_checksum_mismatch_fails_for_restores_and_downloads(self):
        for name in ("collect", "shard", "seed-gitleaks-cache"):
            check = next(step for step in self.jobs[name]["steps"]
                         if "sha256sum" in step.get("run", ""))
            for source in ("restore", "download"):
                with self.subTest(job=name, source=source), tempfile.TemporaryDirectory() as directory:
                    archive = Path(directory) / "gitleaks.tgz"
                    archive.write_bytes(f"corrupt {source}".encode())
                    script = check["run"].replace("$RUNNER_TEMP/gitleaks.tgz", str(archive))
                    result = subprocess.run(
                        ["bash", "-e", "-o", "pipefail", "-c", script],
                        capture_output=True, text=True, timeout=5,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("FAILED", result.stdout)

    def test_actions_are_pinned_and_permissions_are_read_only(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read", "actions": "read"})
        self.assertNotIn("secrets.", self.text)
        actions = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", self.text, re.MULTILINE)
        self.assertTrue(actions)
        for action in actions:
            self.assertRegex(action, r"^[\w./-]+@[0-9a-f]{40}$")
            self.assertIn(action.split("@")[0], {"actions/checkout", "astral-sh/setup-uv",
                                                 "actions/upload-artifact", "actions/download-artifact",
                                                 "actions/cache/restore", "actions/cache/save"})
        for name, job in self.jobs.items():
            if name == "seed-gitleaks-cache":
                self.assertEqual(job["permissions"], {"contents": "read"})
            else:
                self.assertNotIn("permissions", job)
            self.assertNotIn("secrets", job)

    def test_reuse_is_first_and_runs_on_every_event(self):
        self.assertEqual(next(iter(self.jobs)), "reuse")
        reuse = self.jobs["reuse"]
        self.assertNotIn("if", reuse)
        self.assertEqual(reuse["outputs"], {"skip": "${{ steps.decide.outputs.skip }}"})
        self.assertEqual(self.jobs["collect"]["needs"], "reuse")
        self.assertNotIn("needs", self.jobs["lock"])
        decide = next(step for step in reuse["steps"] if step.get("id") == "decide")
        self.assertNotIn("if", decide)
        self.assertEqual(decide["env"], {"GH_TOKEN": "${{ github.token }}",
                                         "GH_REPO": "${{ github.repository }}"})
        script = decide["run"]
        for text in ('actions/workflows/ci.yml/runs', '-f event=workflow_dispatch',
                     '-f head_sha="$SHA"', '--paginate --slurp', '/jobs?filter=latest&per_page=100',
                     'tools/ci_reuse.py --runs', '--jobs', '--sha "$SHA"', 'tee -a "$GITHUB_OUTPUT"'):
            self.assertIn(text, script)

    def test_reuse_only_calls_the_api_for_a_push_to_main(self):
        decide = next(step for step in self.jobs["reuse"]["steps"] if step.get("id") == "decide")
        events = (("workflow_dispatch", "refs/heads/main"),
                  ("workflow_dispatch", "refs/heads/candidate"),
                  ("schedule", "refs/heads/main"),
                  ("push", "refs/heads/feature"),
                  ("push", "refs/tags/v1"),
                  ("push", "refs/heads/main"))
        for event, ref in events:
            with self.subTest(event=event, ref=ref), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                output = temp / "output"
                calls = temp / "api-calls"
                env = {**os.environ, "GITHUB_EVENT_NAME": event, "GITHUB_REF": ref,
                       "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(temp),
                       "GH_REPO": "owner/repo", "SHA": "a" * 40, "API_CALLS": str(calls)}
                # Simulate an API error: main must look up evidence and fall back to testing.
                stub = 'gh() { echo "$*" >> "$API_CALLS"; return 1; }\n'
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", stub + decide["run"]],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                lines = output.read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines[0], "skip=false")
                self.assertEqual(len(lines), 2)
                self.assertRegex(lines[1], r"^reason=.+")
                if event == "push" and ref == "refs/heads/main":
                    self.assertIn("actions/workflows/ci.yml/runs", calls.read_text(encoding="utf-8"))
                else:
                    self.assertFalse(calls.exists(), "Only a push to main may call the API")
                    self.assertFalse((temp / "ci-reuse").exists())

    def test_separate_sharded_workflow_is_removed(self):
        self.assertFalse((ROOT / ".github/workflows/suite-sharded.yml").exists())


if __name__ == "__main__":
    unittest.main()


class CIFinalJobCancellationTests(unittest.TestCase):
    def setUp(self):
        self.final = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["test"]

    def test_final_job_runs_even_when_workflow_is_cancelled(self):
        self.assertEqual(self.final["if"], "${{ always() && " + DISPATCH_ONLY + " && (" + ELIGIBLE + ") && (" + REUSE_ALLOWED + ") }}")
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
        # A status function prevents GitHub's implicit success() dependency gate.
        self.assertEqual(set(self.final["needs"]), {"reuse", "collect", "shard"})
        self.assertTrue(self.final["if"].startswith("${{ always() && " + DISPATCH_ONLY + " && ("))
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
