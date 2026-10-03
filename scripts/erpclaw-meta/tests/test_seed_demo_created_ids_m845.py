"""m845: the demo seed reads each created record's id from the response its action returns.

``seed-demo-data`` used to read ``campaign_id`` / ``sla_id`` / ``project_id``
flat keys that those actions never return, so the summary counted 0 and the
project tasks were never created. The seed now reads every created id through
``_created_id``. These tests pin the fix without running the whole seed:

- unit: ``_created_id`` against the literal payload shapes of ``add-campaign``,
  ``add-sla`` and ``add-project``, a legacy flat-key payload, and a payload
  with neither (returns None);
- integration: each of those three actions driven once on a temp SQLite
  through the seed's own ``_seed_run_skill`` returns an id equal to the row
  now stored in the table;
- phase 13: ``_seed_demo_projects`` with a stubbed runner shaped like the
  real ``add-project`` response issues exactly four ``add-task`` calls with
  the project id and the four literal task names, and the summary counts
  ``projects == 1`` and ``tasks == 4``.
"""
import argparse
import importlib.util
import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_META_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_META_DIR)
_SETUP_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup")
_META_DBQUERY = os.path.join(_META_DIR, "db_query.py")
_INIT_SCHEMA = os.path.join(_SETUP_DIR, "init_schema.py")
_ADDONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(_SCRIPTS_DIR)), "erpclaw-addons")

# Bind erpclaw_lib to the tree under test, never a deployed lib symlink.
_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib")):
    _LIB = _IN_TREE_LIB
else:
    _LIB = os.path.join(os.path.expanduser(
        os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib")
if _LIB not in sys.path:
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, _LIB)

from erpclaw_lib.db import get_connection  # noqa: E402


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


META = _load_module("db_query_meta_m845", _META_DBQUERY)


def _isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ERPCLAW_HOME", str(home / "erpclaw"))
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    return home


def _fresh_sqlite(tmp_path, monkeypatch):
    """Isolated HOME plus a fresh temp SQLite; children inherit it via env."""
    home = _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    db_path = str(tmp_path / "seed_m845.sqlite")
    monkeypatch.setenv("ERPCLAW_DB_PATH", db_path)
    old_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        _LIB + (os.pathsep + old_pythonpath if old_pythonpath else ""),
    )
    _load_module("init_schema_m845", _INIT_SCHEMA).init_db(db_path)
    return home, db_path


def _arrange_addons(home, monkeypatch, skill_name):
    """Link ops/growth addon sources the way test_seed_demo_backend_m840 does.

    Skips with an explicit reason when the addon sources — or the skill
    itself after linking — are not reachable; never passes silently.
    """
    ops_src = os.path.join(_ADDONS_DIR, "erpclaw-ops")
    growth_src = os.path.join(_ADDONS_DIR, "erpclaw-growth")
    if not (os.path.isdir(ops_src) and os.path.isdir(growth_src)):
        pytest.skip(
            "addon sources absent from this tree "
            "(erpclaw-ops / erpclaw-growth not under source/erpclaw-addons)"
        )
    skills = home / "clawd" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    for src, name in ((ops_src, "erpclaw-ops"), (growth_src, "erpclaw-growth")):
        link = os.path.join(str(skills), name)
        if not os.path.islink(link) and not os.path.exists(link):
            os.symlink(src, link)
    monkeypatch.setattr(META, "SKILLS_DIR", str(skills))
    if META._seed_find_skill_script(skill_name) is None:
        pytest.skip(f"{skill_name} not reachable after linking addons")


# ---------------------------------------------------------------------------
# _created_id unit tests — literal payload shapes from each action's ok(...)
# ---------------------------------------------------------------------------

def test_created_id_campaign_shape():
    """Real add-campaign shape: {"campaign": {"id": ...}, "message": ...}."""
    result = {
        "campaign": {
            "id": "camp-0001",
            "name": "Q1 2026 Product Launch",
            "campaign_type": "event",
            "budget": "0",
            "status": "planned",
            "start_date": "2026-01-01",
            "end_date": "2026-03-31",
        },
        "message": "Campaign 'Q1 2026 Product Launch' created",
    }
    assert META._created_id(result, "campaign", "campaign_id") == "camp-0001"


def test_created_id_sla_shape():
    """Real add-sla shape: {"sla": {...}, "message": ...} (no flat sla_id)."""
    result = {
        "sla": {
            "id": "sla-0001",
            "name": "Standard SLA",
            "priority_response_times": {"low": "48", "medium": "24",
                                        "high": "8", "critical": "4"},
            "priority_resolution_times": {"low": "120", "medium": "72",
                                          "high": "24", "critical": "8"},
            "working_hours": None,
            "is_default": 1,
        },
        "message": "SLA 'Standard SLA' created",
    }
    assert META._created_id(result, "sla", "sla_id") == "sla-0001"


def test_created_id_project_shape():
    """Real add-project shape: {"project": <row dict with id>}."""
    result = {
        "project": {
            "id": "proj-0001",
            "naming_series": "PRJ-00001",
            "project_name": "ERP Migration Project",
            "customer_id": None,
            "project_type": "internal",
            "status": "open",
            "priority": "medium",
            "start_date": "2026-01-01",
            "end_date": "2026-06-30",
            "estimated_cost": "0",
            "actual_cost": "0",
            "billing_type": "non_billable",
            "total_billed": "0",
            "profit_margin": "0",
            "percent_complete": "0",
            "cost_center_id": None,
            "company_id": "co-0001",
        },
    }
    assert META._created_id(result, "project", "project_id") == "proj-0001"


def test_created_id_legacy_flat_key():
    """Flat legacy payloads (e.g. add-cost-center) still resolve."""
    result = {"status": "created", "cost_center_id": "cc-0001", "name": "Admin"}
    assert META._created_id(result, "cost_center", "cost_center_id") == "cc-0001"


def test_created_id_neither_returns_none():
    """A payload with neither shape yields None so the phase counts zero."""
    assert META._created_id({"status": "created", "name": "x"},
                            "campaign", "campaign_id") is None
    assert META._created_id({}, "campaign", "campaign_id") is None
    assert META._created_id(None, "campaign", "campaign_id") is None


# ---------------------------------------------------------------------------
# Real actions through the seed's own runner, id compared against the table
# ---------------------------------------------------------------------------

def test_seed_runner_campaign_id_matches_row(tmp_path, monkeypatch):
    """add-campaign via _seed_run_skill: helper id equals the stored row."""
    home, db_path = _fresh_sqlite(tmp_path, monkeypatch)
    _arrange_addons(home, monkeypatch, "erpclaw-crm")
    result = META._seed_run_skill(
        "erpclaw-crm", "add-campaign", db_path,
        name="Q1 2026 Product Launch", campaign_type="event",
        start_date="2026-01-01", end_date="2026-03-31")
    campaign_id = META._created_id(result, "campaign", "campaign_id")
    assert campaign_id, result
    check = get_connection(db_path)
    try:
        row = check.execute(
            "SELECT id FROM campaign WHERE name = ?",
            ("Q1 2026 Product Launch",),
        ).fetchone()
    finally:
        check.close()
    assert row is not None, "campaign row missing after add-campaign"
    assert campaign_id == row["id"]


def test_seed_runner_sla_id_matches_row(tmp_path, monkeypatch):
    """add-sla via _seed_run_skill: helper id equals the stored row."""
    home, db_path = _fresh_sqlite(tmp_path, monkeypatch)
    _arrange_addons(home, monkeypatch, "erpclaw-support")
    priorities = json.dumps({
        "response_times": {"low": "48", "medium": "24",
                           "high": "8", "critical": "4"},
        "resolution_times": {"low": "120", "medium": "72",
                             "high": "24", "critical": "8"},
    })
    result = META._seed_run_skill(
        "erpclaw-support", "add-sla", db_path,
        name="Standard SLA", priorities=priorities, is_default="1")
    sla_id = META._created_id(result, "sla", "sla_id")
    assert sla_id, result
    check = get_connection(db_path)
    try:
        row = check.execute(
            "SELECT id FROM service_level_agreement WHERE name = ?",
            ("Standard SLA",),
        ).fetchone()
    finally:
        check.close()
    assert row is not None, "SLA row missing after add-sla"
    assert sla_id == row["id"]


def test_seed_runner_project_id_matches_row(tmp_path, monkeypatch):
    """add-project via _seed_run_skill: helper id equals the stored row."""
    home, db_path = _fresh_sqlite(tmp_path, monkeypatch)
    _arrange_addons(home, monkeypatch, "erpclaw-projects")
    created = META._seed_run_skill(
        "erpclaw-setup", "setup-company", db_path,
        name="Seed Check Inc.", currency="USD", country="United States",
        fiscal_year_start_month=1)
    company_id = created.get("company_id")
    assert company_id, created
    result = META._seed_run_skill(
        "erpclaw-projects", "add-project", db_path,
        name="ERP Migration Project", company_id=company_id,
        project_type="internal",
        start_date="2026-01-01", end_date="2026-06-30")
    project_id = META._created_id(result, "project", "project_id")
    assert project_id, result
    check = get_connection(db_path)
    try:
        row = check.execute(
            "SELECT id FROM project WHERE project_name = ?",
            ("ERP Migration Project",),
        ).fetchone()
    finally:
        check.close()
    assert row is not None, "project row missing after add-project"
    assert project_id == row["id"]


# ---------------------------------------------------------------------------
# Phase 13 with a stubbed runner
# ---------------------------------------------------------------------------

def test_phase13_creates_four_tasks_with_project_id():
    """Stubbed add-project (real payload shape) gates exactly four tasks."""
    project_id = "proj-seed-1"
    calls = []
    messages = []

    def fake_run_skill(skill_name, action_name, **kwargs):
        calls.append((skill_name, action_name, kwargs))
        if action_name == "add-project":
            assert skill_name == "erpclaw-projects"
            assert kwargs.get("name") == "ERP Migration Project"
            return {"project": {
                "id": project_id,
                "naming_series": "PRJ-00001",
                "project_name": "ERP Migration Project",
                "company_id": kwargs.get("company_id"),
            }}
        if action_name == "add-task":
            return {"task_id": f"task-{len(calls)}", "name": kwargs.get("name")}
        raise AssertionError(f"unexpected call {skill_name}/{action_name}")

    errors = []
    summary = {"projects": 0, "tasks": 0}
    META._seed_demo_projects("company-1", fake_run_skill,
                             messages.append, errors, summary)

    assert errors == []
    assert summary["projects"] == 1
    assert summary["tasks"] == 4
    task_calls = [c for c in calls if c[1] == "add-task"]
    assert len(task_calls) == 4
    assert [c[2]["name"] for c in task_calls] == [
        "Requirements gathering",
        "Data migration",
        "User training",
        "Go-live preparation",
    ]
    assert all(c[0] == "erpclaw-projects" for c in task_calls)
    assert all(c[2]["project_id"] == project_id for c in task_calls)
