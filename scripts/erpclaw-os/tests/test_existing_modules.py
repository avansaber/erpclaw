"""Parameterized validation tests for all existing modules in src/.

Runs validate_module_static() against every real module that has an init_db.py.
All modules must pass. This is the regression gate for the validator itself:
if validate_module_static() is changed and starts failing on real modules,
these tests catch it.
"""
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Import validator
# ---------------------------------------------------------------------------

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(TESTS_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from validate_module import validate_module_static

# ---------------------------------------------------------------------------
# Discover project root and src/
# ---------------------------------------------------------------------------

# tests/ -> erpclaw-os/ -> scripts/ -> erpclaw/ -> src/ -> project-root/
_PROJECT_ROOT = OS_DIR
for _ in range(4):
    _PROJECT_ROOT = os.path.dirname(_PROJECT_ROOT)

SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")


# ---------------------------------------------------------------------------
# Real module paths (relative to SRC_ROOT)
# ---------------------------------------------------------------------------

# Standalone verticals
STANDALONE_MODULES = [
    "legalclaw",
    "retailclaw",
    "constructclaw",
    "agricultureclaw",
    "automotiveclaw",
    "foodclaw",
    "hospitalityclaw",
    "nonprofitclaw",
]

# HealthClaw family
HEALTHCLAW_MODULES = [
    "healthclaw/healthclaw",
    "healthclaw/healthclaw-dental",
    "healthclaw/healthclaw-vet",
    "healthclaw/healthclaw-mental",
    "healthclaw/healthclaw-homehealth",
]

# EducLaw family
EDUCLAW_MODULES = [
    "educlaw/educlaw",
    "educlaw/educlaw-finaid",
    "educlaw/educlaw-k12",
    "educlaw/educlaw-scheduling",
    "educlaw/educlaw-lms",
    "educlaw/educlaw-statereport",
    "educlaw/educlaw-highered",
]

# PropertyClaw family
PROPERTYCLAW_MODULES = [
    "propertyclaw/propertyclaw",
    "propertyclaw/propertyclaw-commercial",
]

# PoC modules (AI-generated)
POC_MODULES = [
    "groomingclaw",
    "tattooclaw",
    "storageclaw",
]

# ERP Addons (grouped repo, each with init_db.py)
ADDON_MODULES = [
    "erpclaw-addons/erpclaw-alerts",
    "erpclaw-addons/erpclaw-approvals",
    "erpclaw-addons/erpclaw-compliance",
    "erpclaw-addons/erpclaw-documents",
    "erpclaw-addons/erpclaw-esign",
    "erpclaw-addons/erpclaw-fleet",
    "erpclaw-addons/erpclaw-growth",
    "erpclaw-addons/erpclaw-integrations",
    "erpclaw-addons/erpclaw-loans",
    "erpclaw-addons/erpclaw-logistics",
    "erpclaw-addons/erpclaw-maintenance",
    "erpclaw-addons/erpclaw-ops",
    "erpclaw-addons/erpclaw-planning",
    "erpclaw-addons/erpclaw-pos",
    "erpclaw-addons/erpclaw-selfservice",
    "erpclaw-addons/erpclaw-treasury",
]

# All modules combined
ALL_MODULES = (
    STANDALONE_MODULES
    + HEALTHCLAW_MODULES
    + EDUCLAW_MODULES
    + PROPERTYCLAW_MODULES
    + POC_MODULES
    + ADDON_MODULES
)


def _abs_module_path(rel_path: str) -> str:
    return os.path.join(SRC_ROOT, rel_path)


def _module_exists(rel_path: str) -> bool:
    """Check if module directory exists and has init_db.py."""
    abs_path = _abs_module_path(rel_path)
    return os.path.isdir(abs_path) and os.path.isfile(os.path.join(abs_path, "init_db.py"))


# ---------------------------------------------------------------------------
# Critical articles that MUST pass for all modules
# ---------------------------------------------------------------------------

# These articles are "never bypass" and critical for data integrity.
# Articles 7, 8, 12 may have some acceptable skips (no scripts, no SKILL.md).
CRITICAL_ARTICLES = [1, 2, 3]


# ---------------------------------------------------------------------------
# Parameterized tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_path", STANDALONE_MODULES,
                         ids=[os.path.basename(m) for m in STANDALONE_MODULES])
def test_standalone_module_passes(module_path):
    """Every standalone vertical module must pass validate_module_static()."""
    abs_path = _abs_module_path(module_path)
    if not _module_exists(module_path):
        pytest.skip(f"Module not found: {abs_path}")

    result = validate_module_static(abs_path, SRC_ROOT)

    for art_num in CRITICAL_ARTICLES:
        art_result = result["articles"].get(art_num)
        if art_result == "fail":
            violations = [v for v in result["violations"] if v.get("article") == art_num]
            pytest.fail(
                f"{module_path} fails Article {art_num}: "
                f"{[v.get('message', str(v)) for v in violations]}"
            )


@pytest.mark.parametrize("module_path", HEALTHCLAW_MODULES,
                         ids=[m.split("/")[-1] for m in HEALTHCLAW_MODULES])
def test_healthclaw_module_passes(module_path):
    """Every HealthClaw module must pass critical articles."""
    abs_path = _abs_module_path(module_path)
    if not _module_exists(module_path):
        pytest.skip(f"Module not found: {abs_path}")

    result = validate_module_static(abs_path, SRC_ROOT)

    for art_num in CRITICAL_ARTICLES:
        art_result = result["articles"].get(art_num)
        if art_result == "fail":
            violations = [v for v in result["violations"] if v.get("article") == art_num]
            pytest.fail(
                f"{module_path} fails Article {art_num}: "
                f"{[v.get('message', str(v)) for v in violations]}"
            )


@pytest.mark.parametrize("module_path", EDUCLAW_MODULES,
                         ids=[m.split("/")[-1] for m in EDUCLAW_MODULES])
def test_educlaw_module_passes(module_path):
    """Every EducLaw module must pass critical articles."""
    abs_path = _abs_module_path(module_path)
    if not _module_exists(module_path):
        pytest.skip(f"Module not found: {abs_path}")

    result = validate_module_static(abs_path, SRC_ROOT)

    for art_num in CRITICAL_ARTICLES:
        art_result = result["articles"].get(art_num)
        if art_result == "fail":
            violations = [v for v in result["violations"] if v.get("article") == art_num]
            pytest.fail(
                f"{module_path} fails Article {art_num}: "
                f"{[v.get('message', str(v)) for v in violations]}"
            )


@pytest.mark.parametrize("module_path", PROPERTYCLAW_MODULES,
                         ids=[m.split("/")[-1] for m in PROPERTYCLAW_MODULES])
def test_propertyclaw_module_passes(module_path):
    """Every PropertyClaw module must pass critical articles."""
    abs_path = _abs_module_path(module_path)
    if not _module_exists(module_path):
        pytest.skip(f"Module not found: {abs_path}")

    result = validate_module_static(abs_path, SRC_ROOT)

    for art_num in CRITICAL_ARTICLES:
        art_result = result["articles"].get(art_num)
        if art_result == "fail":
            violations = [v for v in result["violations"] if v.get("article") == art_num]
            pytest.fail(
                f"{module_path} fails Article {art_num}: "
                f"{[v.get('message', str(v)) for v in violations]}"
            )


@pytest.mark.parametrize("module_path", POC_MODULES,
                         ids=[os.path.basename(m) for m in POC_MODULES])
def test_poc_module_passes(module_path):
    """PoC modules (AI-generated) must pass critical articles."""
    abs_path = _abs_module_path(module_path)
    if not _module_exists(module_path):
        pytest.skip(f"Module not found: {abs_path}")

    result = validate_module_static(abs_path, SRC_ROOT)

    for art_num in CRITICAL_ARTICLES:
        art_result = result["articles"].get(art_num)
        if art_result == "fail":
            violations = [v for v in result["violations"] if v.get("article") == art_num]
            pytest.fail(
                f"{module_path} fails Article {art_num}: "
                f"{[v.get('message', str(v)) for v in violations]}"
            )


@pytest.mark.parametrize("module_path", ADDON_MODULES,
                         ids=[m.split("/")[-1] for m in ADDON_MODULES])
def test_addon_module_passes(module_path):
    """Every ERP addon module must pass critical articles."""
    abs_path = _abs_module_path(module_path)
    if not _module_exists(module_path):
        pytest.skip(f"Module not found: {abs_path}")

    result = validate_module_static(abs_path, SRC_ROOT)

    for art_num in CRITICAL_ARTICLES:
        art_result = result["articles"].get(art_num)
        if art_result == "fail":
            violations = [v for v in result["violations"] if v.get("article") == art_num]
            pytest.fail(
                f"{module_path} fails Article {art_num}: "
                f"{[v.get('message', str(v)) for v in violations]}"
            )


# ---------------------------------------------------------------------------
# Aggregate test: count total modules validated
# ---------------------------------------------------------------------------

def test_minimum_module_coverage():
    """At least 30 real modules should be discoverable and testable."""
    found = sum(1 for m in ALL_MODULES if _module_exists(m))
    assert found >= 30, (
        f"Only found {found} modules with init_db.py — expected at least 30. "
        f"Missing: {[m for m in ALL_MODULES if not _module_exists(m)]}"
    )
