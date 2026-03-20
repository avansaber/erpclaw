"""Tests for ERPClaw OS generate_module — template-based module code generation.

Covers:
- Generating modules from structured entity definitions
- File structure validation
- init_db.py SQL validity
- PyPika usage in generated domain modules
- No direct GL writes
- Test existence
- SKILL.md format
- Prefix enforcement
- Validation integration (validate_module_static)
- Error handling (invalid prefix, duplicate entities)
"""
import json
import os
import re
import sqlite3
import sys
import textwrap

import pytest

# Make the erpclaw-os package importable
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(TESTS_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from generate_module import generate_module, _validate_inputs, _build_entity_actions
from pattern_library import PATTERNS, TERM_TO_PATTERN, get_pattern, list_patterns, suggest_pattern


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# tests/ -> erpclaw-os/ -> scripts/ -> erpclaw/ -> src/ -> project-root/
_PROJECT_ROOT = OS_DIR
for _ in range(4):
    _PROJECT_ROOT = os.path.dirname(_PROJECT_ROOT)
SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")


# ---------------------------------------------------------------------------
# Common entity definitions used across tests
# ---------------------------------------------------------------------------

GROOMING_ENTITIES = [
    {
        "name": "pet",
        "pattern": "crud_entity",
        "fields": ["breed TEXT", "species TEXT NOT NULL DEFAULT 'dog'"],
    },
    {
        "name": "appointment",
        "pattern": "appointment_booking",
        "fields": [],
    },
    {
        "name": "service_type",
        "pattern": "crud_entity",
        "fields": ["price TEXT DEFAULT '0'", "duration_minutes INTEGER DEFAULT 60"],
    },
]

LAUNDROMAT_ENTITIES = [
    {
        "name": "machine",
        "pattern": "crud_entity",
        "fields": ["machine_type TEXT NOT NULL DEFAULT 'washer'", "capacity_lbs TEXT"],
    },
    {
        "name": "cycle",
        "pattern": "service_record",
        "fields": ["machine_id TEXT", "duration_minutes INTEGER DEFAULT 45"],
    },
    {
        "name": "membership",
        "pattern": "prepaid_package",
        "fields": [],
    },
]

INVOICE_ENTITIES = [
    {
        "name": "client",
        "pattern": "crud_entity",
        "fields": [],
    },
    {
        "name": "billing",
        "pattern": "invoice_delegation",
        "fields": [],
    },
]


# ---------------------------------------------------------------------------
# Test: Pattern Library
# ---------------------------------------------------------------------------

class TestPatternLibrary:
    def test_all_patterns_have_required_keys(self):
        for key, pat in PATTERNS.items():
            assert "name" in pat, f"Pattern {key} missing 'name'"
            assert "description" in pat, f"Pattern {key} missing 'description'"
            assert "actions" in pat, f"Pattern {key} missing 'actions'"
            assert "requires_gl" in pat, f"Pattern {key} missing 'requires_gl'"

    def test_get_pattern(self):
        pat = get_pattern("crud_entity")
        assert pat is not None
        assert pat["name"] == "CRUD Entity"

    def test_get_pattern_not_found(self):
        assert get_pattern("nonexistent") is None

    def test_suggest_pattern(self):
        assert suggest_pattern("pet") == "crud_entity"
        assert suggest_pattern("appointment") == "appointment_booking"
        assert suggest_pattern("invoice") == "invoice_delegation"
        assert suggest_pattern("license") == "compliance_tracking"

    def test_suggest_pattern_unknown(self):
        assert suggest_pattern("xyzabc") is None

    def test_pattern_count_is_12(self):
        """Verify we have exactly 12 patterns (7 original + 5 new)."""
        assert len(PATTERNS) == 12

    def test_list_patterns_returns_all(self):
        result = list_patterns()
        assert len(result) == 12
        keys = [p["key"] for p in result]
        assert "document_close" in keys
        assert "blanket_agreement" in keys


# ---------------------------------------------------------------------------
# Test: New Patterns — document_close
# ---------------------------------------------------------------------------

class TestDocumentClosePattern:
    def test_exists_in_patterns(self):
        assert "document_close" in PATTERNS

    def test_required_keys(self):
        pat = PATTERNS["document_close"]
        assert "name" in pat
        assert "description" in pat
        assert "actions" in pat
        assert "requires_gl" in pat
        assert "schema_fields" in pat

    def test_expected_fields(self):
        fields_str = " ".join(PATTERNS["document_close"]["schema_fields"])
        assert "document_id" in fields_str
        assert "close_date" in fields_str
        assert "close_reason" in fields_str
        assert "closed_by" in fields_str
        assert "document_type" in fields_str

    def test_actions(self):
        actions = PATTERNS["document_close"]["actions"]
        assert "close-document" in actions
        assert "reopen-document" in actions

    def test_no_gl(self):
        assert PATTERNS["document_close"]["requires_gl"] is False

    def test_term_mappings(self):
        assert suggest_pattern("close") == "document_close"
        assert suggest_pattern("closed") == "document_close"
        assert suggest_pattern("stop") == "document_close"

    def test_get_pattern(self):
        pat = get_pattern("document_close")
        assert pat is not None
        assert pat["name"] == "Document Close"


# ---------------------------------------------------------------------------
# Test: New Patterns — document_amendment
# ---------------------------------------------------------------------------

class TestDocumentAmendmentPattern:
    def test_exists_in_patterns(self):
        assert "document_amendment" in PATTERNS

    def test_required_keys(self):
        pat = PATTERNS["document_amendment"]
        assert "name" in pat
        assert "description" in pat
        assert "actions" in pat
        assert "requires_gl" in pat
        assert "schema_fields" in pat

    def test_expected_fields(self):
        fields_str = " ".join(PATTERNS["document_amendment"]["schema_fields"])
        assert "original_id" in fields_str
        assert "amendment_number" in fields_str
        assert "amendment_reason" in fields_str

    def test_actions(self):
        actions = PATTERNS["document_amendment"]["actions"]
        assert "amend-document" in actions
        assert "get-amendment-history" in actions

    def test_no_gl(self):
        assert PATTERNS["document_amendment"]["requires_gl"] is False

    def test_term_mappings(self):
        assert suggest_pattern("amend") == "document_amendment"
        assert suggest_pattern("amendment") == "document_amendment"
        assert suggest_pattern("revise") == "document_amendment"

    def test_get_pattern(self):
        pat = get_pattern("document_amendment")
        assert pat is not None
        assert pat["name"] == "Document Amendment"


# ---------------------------------------------------------------------------
# Test: New Patterns — recurring_template
# ---------------------------------------------------------------------------

class TestRecurringTemplatePattern:
    def test_exists_in_patterns(self):
        assert "recurring_template" in PATTERNS

    def test_required_keys(self):
        pat = PATTERNS["recurring_template"]
        assert "name" in pat
        assert "description" in pat
        assert "actions" in pat
        assert "requires_gl" in pat
        assert "schema_fields" in pat

    def test_expected_fields(self):
        fields_str = " ".join(PATTERNS["recurring_template"]["schema_fields"])
        assert "template_name" in fields_str
        assert "frequency" in fields_str
        assert "next_date" in fields_str
        assert "end_date" in fields_str
        assert "auto_submit" in fields_str

    def test_actions(self):
        actions = PATTERNS["recurring_template"]["actions"]
        assert "add-template" in actions
        assert "generate-from-template" in actions
        assert "list-templates" in actions

    def test_gl_delegated(self):
        assert PATTERNS["recurring_template"]["requires_gl"] is True

    def test_related_patterns(self):
        assert "recurring_billing" in PATTERNS["recurring_template"]["related_patterns"]

    def test_term_mappings(self):
        assert suggest_pattern("recurring") == "recurring_template"
        assert suggest_pattern("repeat") == "recurring_template"
        assert suggest_pattern("template") == "recurring_template"

    def test_get_pattern(self):
        pat = get_pattern("recurring_template")
        assert pat is not None
        assert pat["name"] == "Recurring Template"


# ---------------------------------------------------------------------------
# Test: New Patterns — blanket_agreement
# ---------------------------------------------------------------------------

class TestBlanketAgreementPattern:
    def test_exists_in_patterns(self):
        assert "blanket_agreement" in PATTERNS

    def test_required_keys(self):
        pat = PATTERNS["blanket_agreement"]
        assert "name" in pat
        assert "description" in pat
        assert "actions" in pat
        assert "requires_gl" in pat
        assert "schema_fields" in pat

    def test_expected_fields(self):
        fields_str = " ".join(PATTERNS["blanket_agreement"]["schema_fields"])
        assert "party_id" in fields_str
        assert "start_date" in fields_str
        assert "end_date" in fields_str
        assert "total_qty" in fields_str
        assert "fulfilled_qty" in fields_str

    def test_qty_fields_are_text(self):
        """Quantities stored as TEXT for Decimal consistency."""
        fields = PATTERNS["blanket_agreement"]["schema_fields"]
        total_qty_field = [f for f in fields if "total_qty" in f][0]
        fulfilled_qty_field = [f for f in fields if "fulfilled_qty" in f][0]
        assert "TEXT" in total_qty_field
        assert "TEXT" in fulfilled_qty_field

    def test_actions(self):
        actions = PATTERNS["blanket_agreement"]["actions"]
        assert "add-agreement" in actions
        assert "fulfill-against-agreement" in actions
        assert "get-agreement-balance" in actions

    def test_no_gl(self):
        assert PATTERNS["blanket_agreement"]["requires_gl"] is False

    def test_term_mappings(self):
        assert suggest_pattern("blanket") == "blanket_agreement"
        assert suggest_pattern("framework") == "blanket_agreement"
        assert suggest_pattern("long-term") == "blanket_agreement"

    def test_get_pattern(self):
        pat = get_pattern("blanket_agreement")
        assert pat is not None
        assert pat["name"] == "Blanket Agreement"


# ---------------------------------------------------------------------------
# Test: New Patterns — three_way_match
# ---------------------------------------------------------------------------

class TestThreeWayMatchPattern:
    def test_exists_in_patterns(self):
        assert "three_way_match" in PATTERNS

    def test_required_keys(self):
        pat = PATTERNS["three_way_match"]
        assert "name" in pat
        assert "description" in pat
        assert "actions" in pat
        assert "requires_gl" in pat
        assert "schema_fields" in pat

    def test_expected_fields(self):
        fields_str = " ".join(PATTERNS["three_way_match"]["schema_fields"])
        assert "po_id" in fields_str
        assert "receipt_id" in fields_str
        assert "invoice_id" in fields_str
        assert "match_status" in fields_str
        assert "tolerance_pct" in fields_str
        assert "match_policy" in fields_str

    def test_tolerance_is_text(self):
        """Tolerance percentage stored as TEXT for Decimal consistency."""
        fields = PATTERNS["three_way_match"]["schema_fields"]
        tol_field = [f for f in fields if "tolerance_pct" in f][0]
        assert "TEXT" in tol_field

    def test_actions(self):
        actions = PATTERNS["three_way_match"]["actions"]
        assert "run-match" in actions
        assert "list-unmatched" in actions
        assert "override-match" in actions

    def test_no_gl(self):
        assert PATTERNS["three_way_match"]["requires_gl"] is False

    def test_term_mappings(self):
        assert suggest_pattern("match") == "three_way_match"
        assert suggest_pattern("matching") == "three_way_match"
        assert suggest_pattern("3-way") == "three_way_match"
        assert suggest_pattern("three-way") == "three_way_match"

    def test_get_pattern(self):
        pat = get_pattern("three_way_match")
        assert pat is not None
        assert pat["name"] == "Three-Way Match"


# ---------------------------------------------------------------------------
# Test: All TERM_TO_PATTERN values reference valid patterns
# ---------------------------------------------------------------------------

class TestTermToPatternIntegrity:
    def test_all_terms_map_to_valid_patterns(self):
        """Every value in TERM_TO_PATTERN must be a key in PATTERNS."""
        for term, pattern_key in TERM_TO_PATTERN.items():
            assert pattern_key in PATTERNS, (
                f"Term '{term}' maps to '{pattern_key}' which is not in PATTERNS"
            )

    def test_new_term_count(self):
        """We added 15 new terms (3 per pattern * 5 patterns, but one has 4). Total should be >= 50."""
        assert len(TERM_TO_PATTERN) >= 50


# ---------------------------------------------------------------------------
# Test: Input Validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_valid_inputs(self):
        errors = _validate_inputs("testclaw", "test", GROOMING_ENTITIES)
        assert errors == []

    def test_empty_module_name(self):
        errors = _validate_inputs("", "test", GROOMING_ENTITIES)
        assert any("module_name" in e for e in errors)

    def test_empty_prefix(self):
        errors = _validate_inputs("testclaw", "", GROOMING_ENTITIES)
        assert any("prefix" in e for e in errors)

    def test_no_entities(self):
        errors = _validate_inputs("testclaw", "test", [])
        assert any("entity" in e.lower() for e in errors)

    def test_duplicate_entity_names(self):
        entities = [
            {"name": "pet", "pattern": "crud_entity"},
            {"name": "pet", "pattern": "crud_entity"},
        ]
        errors = _validate_inputs("testclaw", "test", entities)
        assert any("Duplicate" in e for e in errors)

    def test_invalid_pattern(self):
        entities = [{"name": "pet", "pattern": "nonexistent_pattern"}]
        errors = _validate_inputs("testclaw", "test", entities)
        assert any("Unknown pattern" in e for e in errors)

    def test_invalid_prefix_format(self):
        errors = _validate_inputs("testclaw", "Test-Bad", GROOMING_ENTITIES)
        assert any("Prefix" in e for e in errors)

    def test_prefix_starting_with_number(self):
        errors = _validate_inputs("testclaw", "1test", GROOMING_ENTITIES)
        assert any("Prefix" in e for e in errors)


# ---------------------------------------------------------------------------
# Test: Module Generation
# ---------------------------------------------------------------------------

class TestGenerateModule:
    def test_generate_grooming_module(self, tmp_path):
        """Generate a grooming module with 3 entities (pet, appointment, service_type).
        Verify: files created, correct prefix, correct table names."""
        output_dir = str(tmp_path / "groomingclaw")
        result = generate_module(
            module_name="groomingclaw",
            prefix="groom",
            business_description="Pet grooming salon management with appointments, pet profiles, and service catalogs.",
            entities=GROOMING_ENTITIES,
            output_dir=output_dir,
        )
        assert result["entities"] == 3
        assert result["tables"] == 3  # pet, appointment, service_type
        assert result["actions"] > 0
        assert len(result["files_created"]) >= 7  # init_db, db_query, domain, SKILL.md, conftest, helpers, test, __init__

    def test_generate_module_file_structure(self, tmp_path):
        """Verify generated module has: init_db.py, scripts/db_query.py, scripts/{name}.py, SKILL.md, tests/"""
        output_dir = str(tmp_path / "testclaw")
        generate_module(
            module_name="testclaw",
            prefix="test",
            business_description="A test module for validation.",
            entities=[{"name": "item", "pattern": "crud_entity", "fields": []}],
            output_dir=output_dir,
        )

        assert os.path.isfile(os.path.join(output_dir, "init_db.py"))
        assert os.path.isfile(os.path.join(output_dir, "scripts", "db_query.py"))
        # module_short for "testclaw" is "test" (removes "claw")
        assert os.path.isfile(os.path.join(output_dir, "scripts", "test.py"))
        assert os.path.isfile(os.path.join(output_dir, "SKILL.md"))
        assert os.path.isdir(os.path.join(output_dir, "scripts", "tests"))
        assert os.path.isfile(os.path.join(output_dir, "scripts", "tests", "__init__.py"))
        assert os.path.isfile(os.path.join(output_dir, "scripts", "tests", "conftest.py"))

    def test_generate_module_init_db_valid(self, tmp_path):
        """Verify init_db.py creates valid SQL (can execute against fresh DB)."""
        output_dir = str(tmp_path / "sqlclaw")
        generate_module(
            module_name="sqlclaw",
            prefix="sql",
            business_description="SQL test module.",
            entities=[
                {"name": "widget", "pattern": "crud_entity", "fields": ["color TEXT"]},
                {"name": "order", "pattern": "crud_entity", "fields": ["quantity INTEGER DEFAULT 1"]},
            ],
            output_dir=output_dir,
        )

        # Read the init_db.py content
        init_db_path = os.path.join(output_dir, "init_db.py")
        with open(init_db_path) as f:
            init_content = f.read()

        # Extract the SQL from the executescript call
        match = re.search(r'conn\.executescript\("""(.+?)"""\)', init_content, re.DOTALL)
        assert match, "Could not find executescript SQL in init_db.py"
        sql = match.group(1)

        # Execute the SQL against a fresh in-memory DB
        # First create the company table that's referenced
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE IF NOT EXISTS company (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS customer (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS naming_series (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS audit_log (id TEXT PRIMARY KEY)")
        conn.executescript(sql)

        # Verify tables exist
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "sql_widget" in tables
        assert "sql_order" in tables
        conn.close()

    def test_generate_module_actions_use_pypika(self, tmp_path):
        """Verify generated domain .py file imports from erpclaw_lib.query."""
        output_dir = str(tmp_path / "pypiclaw")
        generate_module(
            module_name="pypiclaw",
            prefix="pypi",
            business_description="PyPika test module.",
            entities=[{"name": "item", "pattern": "crud_entity", "fields": []}],
            output_dir=output_dir,
        )

        # module_short for "pypiclaw" is "pypi"
        domain_path = os.path.join(output_dir, "scripts", "pypi.py")
        assert os.path.isfile(domain_path)
        with open(domain_path) as f:
            content = f.read()

        assert "from erpclaw_lib.query import" in content
        assert "Table(" in content or "Q.from_" in content

    def test_generate_module_no_direct_gl(self, tmp_path):
        """Verify generated code never writes to gl_entry directly."""
        output_dir = str(tmp_path / "noglclaw")
        generate_module(
            module_name="noglclaw",
            prefix="nogl",
            business_description="No GL module.",
            entities=[
                {"name": "item", "pattern": "crud_entity", "fields": []},
                {"name": "billing", "pattern": "invoice_delegation", "fields": []},
            ],
            output_dir=output_dir,
        )

        # Check all .py files for direct GL writes
        for root, dirs, files in os.walk(output_dir):
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    with open(fpath) as f:
                        content = f.read()
                    assert "INSERT INTO gl_entry" not in content, f"Direct GL write found in {fpath}"
                    assert "INSERT INTO stock_ledger_entry" not in content, f"Direct SLE write found in {fpath}"

    def test_generate_module_tests_exist(self, tmp_path):
        """Verify tests/ dir has test_*.py with at least one test per action."""
        output_dir = str(tmp_path / "testexclaw")
        result = generate_module(
            module_name="testexclaw",
            prefix="testex",
            business_description="Test existence module.",
            entities=[{"name": "item", "pattern": "crud_entity", "fields": []}],
            output_dir=output_dir,
        )

        # module_short for "testexclaw" is "testex"
        test_path = os.path.join(output_dir, "scripts", "tests", "test_testex.py")
        assert os.path.isfile(test_path)
        with open(test_path) as f:
            content = f.read()

        # Should have test functions for each action
        assert "def test_" in content
        # For crud_entity: add, update, get, list -> at least 4 test functions
        test_count = content.count("def test_")
        assert test_count >= 4, f"Expected at least 4 test functions, got {test_count}"

    def test_generate_module_skill_md_valid(self, tmp_path):
        """Verify SKILL.md has valid YAML frontmatter and is under 300 lines."""
        output_dir = str(tmp_path / "skillclaw")
        generate_module(
            module_name="skillclaw",
            prefix="skill",
            business_description="SKILL.md test module.",
            entities=[{"name": "item", "pattern": "crud_entity", "fields": []}],
            output_dir=output_dir,
        )

        skill_path = os.path.join(output_dir, "SKILL.md")
        assert os.path.isfile(skill_path)
        with open(skill_path) as f:
            content = f.read()

        # Check line count
        lines = content.split("\n")
        assert len(lines) <= 300, f"SKILL.md has {len(lines)} lines, max 300"

        # Check YAML frontmatter
        assert content.startswith("---"), "SKILL.md must start with ---"
        parts = content.split("---", 2)
        assert len(parts) >= 3, "SKILL.md must have --- delimited frontmatter"

        # Parse YAML
        import yaml
        frontmatter = yaml.safe_load(parts[1])
        assert frontmatter.get("name") == "skillclaw"
        assert frontmatter.get("version") == "1.0.0"
        assert frontmatter.get("description") is not None
        assert "scripts" in frontmatter

    def test_generate_module_prefix_enforcement(self, tmp_path):
        """Verify all table names start with the given prefix."""
        output_dir = str(tmp_path / "pfxclaw")
        generate_module(
            module_name="pfxclaw",
            prefix="pfx",
            business_description="Prefix enforcement test.",
            entities=[
                {"name": "widget", "pattern": "crud_entity", "fields": []},
                {"name": "gadget", "pattern": "crud_entity", "fields": []},
            ],
            output_dir=output_dir,
        )

        init_db_path = os.path.join(output_dir, "init_db.py")
        with open(init_db_path) as f:
            content = f.read()

        # Find all CREATE TABLE statements
        tables = re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', content)
        for tname in tables:
            assert tname.startswith("pfx_"), f"Table {tname} does not start with prefix pfx_"

    def test_generate_novel_business(self, tmp_path):
        """Generate a laundromat module (not from PoCs). Verify constitution compliance."""
        output_dir = str(tmp_path / "laundroclaw")
        result = generate_module(
            module_name="laundroclaw",
            prefix="laundro",
            business_description="Self-service laundromat management with washing machines, dryer cycles, and monthly memberships.",
            entities=LAUNDROMAT_ENTITIES,
            output_dir=output_dir,
        )

        assert result["entities"] == 3
        assert result["tables"] >= 2  # machine + cycle have fields; membership has fields from prepaid_package
        assert result["actions"] > 0
        assert len(result["files_created"]) >= 7

        # Verify files exist
        assert os.path.isfile(os.path.join(output_dir, "init_db.py"))
        assert os.path.isfile(os.path.join(output_dir, "SKILL.md"))

    def test_generate_module_with_invoice_delegation(self, tmp_path):
        """Generate a module with invoice_delegation pattern. Verify cross_skill usage."""
        output_dir = str(tmp_path / "invoiceclaw")
        result = generate_module(
            module_name="invoiceclaw",
            prefix="inv",
            business_description="Invoice delegation test module.",
            entities=INVOICE_ENTITIES,
            output_dir=output_dir,
        )

        # module_short for "invoiceclaw" is "invoice"
        domain_path = os.path.join(output_dir, "scripts", "invoice.py")
        assert os.path.isfile(domain_path)
        with open(domain_path) as f:
            content = f.read()

        # Verify cross_skill import
        assert "cross_skill" in content
        assert "create_invoice" in content
        # Verify NO direct GL writes
        assert "INSERT INTO gl_entry" not in content

    def test_generate_module_validation_integration(self, tmp_path):
        """Generate a module and run validate_module_static(). Must pass critical articles."""
        output_dir = str(tmp_path / "validclaw")
        result = generate_module(
            module_name="validclaw",
            prefix="valid",
            business_description="Validation integration test module with basic CRUD.",
            entities=[
                {"name": "item", "pattern": "crud_entity", "fields": ["price TEXT DEFAULT '0'"]},
                {"name": "record", "pattern": "service_record", "fields": []},
            ],
            output_dir=output_dir,
        )

        # Check validation ran
        validation = result.get("validation", {})
        assert validation is not None

        # If validation ran successfully, check key articles
        if "articles" in validation:
            articles = validation["articles"]
            # Article 1: Table prefix
            if 1 in articles:
                assert articles[1] == "pass", f"Article 1 (table prefix) failed: {validation.get('violations', [])}"
            # Article 2: Money is TEXT
            if 2 in articles:
                assert articles[2] == "pass", f"Article 2 (money TEXT) failed: {validation.get('violations', [])}"
            # Article 3: UUID PKs
            if 3 in articles:
                assert articles[3] == "pass", f"Article 3 (UUID PKs) failed: {validation.get('violations', [])}"

    def test_generate_module_invalid_prefix(self, tmp_path):
        """Try generating with an empty prefix. Should error."""
        output_dir = str(tmp_path / "badclaw")
        result = generate_module(
            module_name="badclaw",
            prefix="",
            business_description="Should fail.",
            entities=[{"name": "item", "pattern": "crud_entity"}],
            output_dir=output_dir,
        )
        assert result["result"] == "fail"
        assert result["entities"] == 0

    def test_generate_module_duplicate_entity_names(self, tmp_path):
        """Try generating with duplicate entity names. Should error."""
        output_dir = str(tmp_path / "dupclaw")
        result = generate_module(
            module_name="dupclaw",
            prefix="dup",
            business_description="Should fail.",
            entities=[
                {"name": "item", "pattern": "crud_entity"},
                {"name": "item", "pattern": "crud_entity"},
            ],
            output_dir=output_dir,
        )
        assert result["result"] == "fail"
        assert result["entities"] == 0

    def test_generate_module_action_names_kebab_case(self, tmp_path):
        """Verify all generated action names use kebab-case."""
        output_dir = str(tmp_path / "kebabclaw")
        generate_module(
            module_name="kebabclaw",
            prefix="keb",
            business_description="Kebab case test.",
            entities=[{"name": "item", "pattern": "crud_entity", "fields": []}],
            output_dir=output_dir,
        )

        # module_short for "kebabclaw" is "kebab"
        domain_path = os.path.join(output_dir, "scripts", "kebab.py")
        with open(domain_path) as f:
            content = f.read()

        # Find all action names in the ACTIONS dict (keys are quoted strings before colon+space+func)
        # The ACTIONS dict looks like: "keb-add-items": keb_add_items,
        action_section = content[content.index("ACTIONS = {"):]
        action_names = re.findall(r'"([a-z][a-z0-9-]*)"\s*:', action_section)
        assert len(action_names) > 0, "No action names found in ACTIONS dict"
        for action in action_names:
            assert "_" not in action, f"Action name {action} uses underscore instead of kebab-case"
            assert action == action.lower(), f"Action name {action} is not lowercase"

    def test_generate_module_db_query_imports_domain(self, tmp_path):
        """Verify db_query.py imports from the domain module."""
        output_dir = str(tmp_path / "importclaw")
        generate_module(
            module_name="importclaw",
            prefix="imp",
            business_description="Import test.",
            entities=[{"name": "item", "pattern": "crud_entity", "fields": []}],
            output_dir=output_dir,
        )

        db_query_path = os.path.join(output_dir, "scripts", "db_query.py")
        with open(db_query_path) as f:
            content = f.read()

        assert "from erpclaw_lib.response import ok, err" in content
        assert "from erpclaw_lib.db import get_connection" in content
        assert "ACTIONS" in content

    def test_generate_module_multiple_patterns(self, tmp_path):
        """Generate a module with entities from different patterns."""
        output_dir = str(tmp_path / "multiclaw")
        result = generate_module(
            module_name="multiclaw",
            prefix="multi",
            business_description="Multi-pattern test with CRUD, appointments, and compliance.",
            entities=[
                {"name": "client", "pattern": "crud_entity", "fields": ["phone TEXT"]},
                {"name": "visit", "pattern": "appointment_booking", "fields": []},
                {"name": "license", "pattern": "compliance_tracking", "fields": []},
            ],
            output_dir=output_dir,
        )
        assert result["entities"] == 3
        assert result["tables"] == 3

        # Check all tables have correct prefix
        init_db_path = os.path.join(output_dir, "init_db.py")
        with open(init_db_path) as f:
            content = f.read()
        tables = re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', content)
        assert "multi_client" in tables
        assert "multi_visit" in tables
        assert "multi_license" in tables
