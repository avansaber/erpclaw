"""CI uses the mini PC runner without binding its own PostgreSQL port."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_push_is_limited_to_main():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))  # PyYAML reads 'on' as True.
    assert triggers["push"] == {"branches": ["main"]}


def test_shard_matrix_matches_shards_count():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    shards = int(workflow["env"]["SHARDS"])
    indices = workflow["jobs"]["shard"]["strategy"]["matrix"]["index"]
    assert len(indices) == shards
    assert indices == list(range(shards))


@pytest.mark.parametrize("name", ("collect", "shard", "test"))
def test_test_jobs_allow_three_hours(name):
    assert _jobs()[name]["timeout-minutes"] == 180


def test_jobs_run_on_the_ci_machine_or_light_runner():
    jobs = _jobs()
    assert jobs["test"]["runs-on"] == ["self-hosted", "light"]
    for name in ("reuse", "seed-gitleaks-cache", "lock", "collect", "shard"):
        assert jobs[name]["runs-on"] == ["self-hosted", "erpclaw-ci"], name
    for name, job in jobs.items():
        assert job["runs-on"][0] == "self-hosted", name
        assert "erpclaw-mini" not in job["runs-on"], name


def test_no_run_or_path_uses_a_fixed_tmp_path():
    def check(value, location):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("run", "path"):
                    assert "/tmp/" not in str(child), (location, key, child)
                check(child, (*location, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                check(child, (*location, index))

    check(_jobs(), ())


def test_no_service_maps_host_port_5432():
    for name, job in _jobs().items():
        for service in job.get("services", {}).values():
            for mapping in service.get("ports", []):
                assert isinstance(mapping, str), (name, mapping)
                if ":" in mapping:
                    host, _ = mapping.rsplit(":", 1)
                    assert host.rsplit(":", 1)[-1] != "5432", (name, mapping)


def test_no_service_maps_a_fixed_host_port():
    for name, job in _jobs().items():
        for service in job.get("services", {}).values():
            for mapping in service.get("ports", []):
                assert isinstance(mapping, str), (name, mapping)
                assert ":" not in mapping, (name, mapping)


@pytest.mark.parametrize("name", ("collect", "shard"))
def test_each_postgres_service_maps_an_ephemeral_host_port(name):
    assert _jobs()[name]["services"]["postgres"]["ports"] == ["5432"]


def test_each_pg_url_uses_its_jobs_host_port():
    port_expression = "${{ job.services.postgres.ports['5432'] }}"
    for name, job in _jobs().items():
        environments = [job.get("env", {}), *(step.get("env", {}) for step in job.get("steps", []))]
        urls = [env["ERPCLAW_PG_TEST_URL"] for env in environments if "ERPCLAW_PG_TEST_URL" in env]
        postgres = job.get("services", {}).get("postgres")
        if postgres is not None:
            assert urls, name
        if not urls:
            continue
        assert postgres is not None, name
        assert len(postgres["ports"]) == 1, name
        assert postgres["ports"] == ["5432"], name
        for url in urls:
            assert port_expression in url, (name, url)
            parsed = urlsplit(url.replace(port_expression, "49152"))
            assert parsed.hostname == "127.0.0.1", name
            assert parsed.port == 49152, (name, url)
