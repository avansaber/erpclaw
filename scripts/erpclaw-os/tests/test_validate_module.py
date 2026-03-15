"""Tests for ERPClaw OS validate_module — covers all 11 static articles.

For each article: at minimum one test where a valid module passes
and one test where a violation is detected.

Also tests against real existing modules (legalclaw, retailclaw, healthclaw-vet)
and the table ownership registry builder.
"""
import os
import sys
import textwrap

import pytest

# Make the erpclaw-os package importable
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(TESTS_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from validate_module import (
    validate_module_static,
    build_table_ownership_registry,
    _check_article_1,
    _check_article_2,
    _check_article_3,
    _check_article_4,
    _check_article_5,
    _check_article_6,
    _check_article_7,
    _check_article_8,
    _check_article_10,
    _check_article_11,
    _check_article_12,
    _extract_tables_from_ddl,
    _parse_columns,
    _extract_action_names_from_skill_md,
    _derive_module_name,
    _is_core_module,
)
from constitution import ARTICLES, get_static_articles, get_runtime_articles, get_article


# ---------------------------------------------------------------------------
# Paths to real modules for integration testing
# ---------------------------------------------------------------------------

# tests/ -> erpclaw-os/ -> scripts/ -> erpclaw/ -> src/ -> project-root/
_PROJECT_ROOT = OS_DIR
for _ in range(4):
    _PROJECT_ROOT = os.path.dirname(_PROJECT_ROOT)

SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")
LEGALCLAW_PATH = os.path.join(SRC_ROOT, "legalclaw")
RETAILCLAW_PATH = os.path.join(SRC_ROOT, "retailclaw")
HEALTHCLAW_VET_PATH = os.path.join(SRC_ROOT, "healthclaw", "healthclaw-vet")


# ---------------------------------------------------------------------------
# Constitution tests
# ---------------------------------------------------------------------------

class TestConstitution:
    """Test the constitution data structure."""

    def test_all_18_articles_present(self):
        assert len(ARTICLES) == 18

    def test_article_numbers_sequential(self):
        for i, article in enumerate(ARTICLES):
            assert article["number"] == i + 1

    def test_static_articles_count(self):
        static = get_static_articles()
        # Articles 1-8, 10-12 are static (11 total)
        assert len(static) == 11

    def test_runtime_articles_count(self):
        runtime = get_runtime_articles()
        # Articles 9, 13-18 are runtime (7 total)
        assert len(runtime) == 7

    def test_get_article_by_number(self):
        art = get_article(1)
        assert art is not None
        assert art["name"] == "Table Prefix Enforcement"

    def test_get_article_nonexistent(self):
        assert get_article(99) is None

    def test_all_articles_have_required_fields(self):
        for art in ARTICLES:
            assert "number" in art
            assert "name" in art
            assert "description" in art
            assert "enforcement" in art
            assert "severity" in art
            assert "bypass_policy" in art

    def test_enforcement_values(self):
        for art in ARTICLES:
            assert art["enforcement"] in ("static", "runtime")

    def test_severity_values(self):
        for art in ARTICLES:
            assert art["severity"] in ("critical", "warning")

    def test_bypass_policy_values(self):
        for art in ARTICLES:
            assert art["bypass_policy"] in ("never", "tier2", "tier3")


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelpers:
    """Test helper functions."""

    def test_extract_tables_from_ddl(self):
        ddl = """
        CREATE TABLE IF NOT EXISTS foo_bar (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS foo_baz (id TEXT PRIMARY KEY);
        """
        tables = _extract_tables_from_ddl(ddl)
        assert tables == ["foo_bar", "foo_baz"]

    def test_parse_columns(self):
        ddl = """
        CREATE TABLE IF NOT EXISTS test_item (
            id      TEXT PRIMARY KEY,
            name    TEXT NOT NULL,
            price   REAL NOT NULL
        );
        """
        cols = _parse_columns(ddl)
        assert len(cols) == 3
        assert cols[0]["name"] == "id"
        assert cols[0]["type"] == "TEXT"
        assert cols[0]["is_pk"] is True
        assert cols[2]["name"] == "price"
        assert cols[2]["type"] == "REAL"

    def test_derive_module_name(self):
        assert _derive_module_name("/path/to/src/legalclaw") == "legalclaw"
        assert _derive_module_name("/path/to/src/healthclaw/healthclaw-vet") == "healthclaw-vet"

    def test_extract_action_names_from_skill_md(self):
        skill_md = textwrap.dedent("""\
            ---
            name: testclaw
            version: 1.0.0
            description: Test
            ---
            # test

            | Action | Description |
            |--------|-------------|
            | `add-widget` | Add widget |
            | `list-widgets` | List widgets |
            | `status` | Status |
        """)
        actions = _extract_action_names_from_skill_md(skill_md)
        assert "add-widget" in actions
        assert "list-widgets" in actions
        assert "status" in actions


# ---------------------------------------------------------------------------
# Article 1: Table Prefix Enforcement
# ---------------------------------------------------------------------------

class TestArticle1:
    """Article 1: Table Prefix Enforcement."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_1(str(temp_module_dir), "testclaw")
        assert result["result"] == "pass"

    def test_violation_detected(self, violation_module):
        path = violation_module(article=1)
        result = _check_article_1(path, "violclaw")
        assert result["result"] == "fail"
        assert len(result["violations"]) > 0
        assert "appointment" in result["violations"][0]["table"]

    def test_no_init_db_skips(self, tmp_path):
        module = tmp_path / "noclaw"
        module.mkdir()
        result = _check_article_1(str(module), "noclaw")
        assert result["result"] == "skip"

    def test_multiple_valid_prefixes(self, tmp_path):
        """A module can use either full name or short prefix."""
        module = tmp_path / "groomingclaw"
        module.mkdir()
        (module / "init_db.py").write_text(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS groom_pet (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groom_appointment (
                    id TEXT PRIMARY KEY,
                    pet_id TEXT REFERENCES groom_pet(id)
                );
            \"\"\"
        """))
        result = _check_article_1(str(module), "groomingclaw")
        assert result["result"] == "pass"


# ---------------------------------------------------------------------------
# Article 2: Money is TEXT
# ---------------------------------------------------------------------------

class TestArticle2:
    """Article 2: Money is TEXT."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_2(str(temp_module_dir))
        assert result["result"] == "pass"

    def test_real_violation_detected(self, violation_module):
        path = violation_module(article=2)
        result = _check_article_2(path)
        assert result["result"] == "fail"
        assert any("REAL" in v.get("type", "") or "FLOAT" in v.get("type", "") for v in result["violations"])

    def test_text_money_columns_pass(self, tmp_path):
        module = tmp_path / "moneyclaw"
        module.mkdir()
        (module / "init_db.py").write_text(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS moneyclaw_invoice (
                    id          TEXT PRIMARY KEY,
                    amount      TEXT NOT NULL DEFAULT '0',
                    tax         TEXT NOT NULL DEFAULT '0',
                    discount    TEXT NOT NULL DEFAULT '0',
                    total       TEXT NOT NULL DEFAULT '0',
                    balance     TEXT NOT NULL DEFAULT '0'
                );
            \"\"\"
        """))
        result = _check_article_2(str(module))
        assert result["result"] == "pass"


# ---------------------------------------------------------------------------
# Article 3: UUID Primary Keys
# ---------------------------------------------------------------------------

class TestArticle3:
    """Article 3: UUID Primary Keys (id TEXT)."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_3(str(temp_module_dir))
        assert result["result"] == "pass"

    def test_integer_pk_violation(self, violation_module):
        path = violation_module(article=3)
        result = _check_article_3(path)
        assert result["result"] == "fail"
        assert any("INTEGER" in v.get("type", "") for v in result["violations"])

    def test_composite_pk_accepted(self, tmp_path):
        """Composite primary keys are valid for join tables."""
        module = tmp_path / "joinclaw"
        module.mkdir()
        (module / "init_db.py").write_text(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS joinclaw_mapping (
                    item_id     TEXT NOT NULL,
                    tag_id      TEXT NOT NULL,
                    PRIMARY KEY (item_id, tag_id)
                );
            \"\"\"
        """))
        result = _check_article_3(str(module))
        assert result["result"] == "pass"


# ---------------------------------------------------------------------------
# Article 4: Foreign Key Integrity
# ---------------------------------------------------------------------------

class TestArticle4:
    """Article 4: Foreign Key Integrity."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_4(str(temp_module_dir))
        # Internal FKs (testclaw_order -> testclaw_item) should pass
        assert result["result"] == "pass"

    def test_nonexistent_fk_violation(self, violation_module):
        path = violation_module(article=4)
        result = _check_article_4(path)
        assert result["result"] == "fail"
        assert any("nonexistent_table" in v.get("referenced_table", "") for v in result["violations"])

    def test_core_table_fk_passes(self, tmp_path, src_root):
        """FK to core tables (company, customer) should pass if src_root provided."""
        module = tmp_path / "fkclaw"
        module.mkdir()
        (module / "init_db.py").write_text(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS fkclaw_ext (
                    id          TEXT PRIMARY KEY,
                    company_id  TEXT NOT NULL REFERENCES company(id),
                    customer_id TEXT REFERENCES customer(id)
                );
            \"\"\"
        """))
        result = _check_article_4(str(module), src_root)
        assert result["result"] == "pass"


# ---------------------------------------------------------------------------
# Article 5: No Cross-Module Writes
# ---------------------------------------------------------------------------

class TestArticle5:
    """Article 5: No Cross-Module Writes."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_5(str(temp_module_dir))
        assert result["result"] in ("pass", "skip")

    def test_cross_write_violation(self, violation_module, src_root):
        path = violation_module(article=5)
        # Build table registry from real src
        registry = build_table_ownership_registry(src_root)
        result = _check_article_5(path, registry)
        assert result["result"] == "fail"
        assert any("customer" in v.get("target_table", "") or "account" in v.get("target_table", "")
                    for v in result["violations"])

    def test_own_table_write_passes(self, tmp_path):
        """Writing to own tables is fine."""
        module = tmp_path / "ownclaw"
        module.mkdir()
        (module / "init_db.py").write_text(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS ownclaw_item (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
            \"\"\"
        """))
        scripts_dir = module / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "db_query.py").write_text(textwrap.dedent("""\
            import os, sys, json, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err

            def add_item(conn):
                conn.execute("INSERT INTO ownclaw_item (id, name) VALUES (?, ?)", ("1", "test"))
                ok({"message": "done"})
        """))
        result = _check_article_5(str(module))
        assert result["result"] == "pass"


# ---------------------------------------------------------------------------
# Article 6: No Direct GL Writes
# ---------------------------------------------------------------------------

class TestArticle6:
    """Article 6: No Direct GL Writes."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_6(str(temp_module_dir))
        assert result["result"] == "pass"

    def test_gl_write_violation(self, violation_module):
        path = violation_module(article=6)
        result = _check_article_6(path)
        assert result["result"] == "fail"
        assert len(result["violations"]) >= 1

    def test_gl_posting_lib_import_is_fine(self, tmp_path):
        """Using erpclaw_lib.gl_posting is the correct way."""
        module = tmp_path / "glclaw"
        module.mkdir()
        scripts_dir = module / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "db_query.py").write_text(textwrap.dedent("""\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            from erpclaw_lib.gl_posting import insert_gl_entries

            def post(conn, entries):
                insert_gl_entries(conn, entries)
                ok({"message": "posted via gl_posting"})
        """))
        result = _check_article_6(str(module))
        assert result["result"] == "pass"


# ---------------------------------------------------------------------------
# Article 7: Response Format (ok/err)
# ---------------------------------------------------------------------------

class TestArticle7:
    """Article 7: Response Format."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_7(str(temp_module_dir))
        assert result["result"] == "pass"

    def test_missing_ok_err_violation(self, violation_module):
        path = violation_module(article=7)
        result = _check_article_7(path)
        assert result["result"] == "fail"

    def test_delegated_imports_pass(self, tmp_path):
        """ok/err imported in a submodule (not db_query.py) should still pass."""
        module = tmp_path / "delclaw"
        module.mkdir()
        scripts_dir = module / "scripts"
        scripts_dir.mkdir()
        # db_query.py imports from handler
        (scripts_dir / "db_query.py").write_text(textwrap.dedent("""\
            import os, sys
            from handler import handle
            handle()
        """))
        # handler.py imports ok/err
        (scripts_dir / "handler.py").write_text(textwrap.dedent("""\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err

            def handle():
                ok({"message": "delegated"})
        """))
        result = _check_article_7(str(module))
        assert result["result"] == "pass"


# ---------------------------------------------------------------------------
# Article 8: Tests Exist
# ---------------------------------------------------------------------------

class TestArticle8:
    """Article 8: Tests Exist."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_8(str(temp_module_dir))
        assert result["result"] == "pass"

    def test_missing_tests_violation(self, violation_module):
        path = violation_module(article=8)
        result = _check_article_8(path)
        assert result["result"] == "fail"
        # Should report untested actions
        assert any("untested_actions" in v for v in result["violations"])

    def test_no_tests_dir_fails(self, tmp_path):
        module = tmp_path / "notestclaw"
        module.mkdir()
        (module / "SKILL.md").write_text("---\nname: notestclaw\nversion: 1.0.0\ndescription: x\n---\n")
        result = _check_article_8(str(module))
        assert result["result"] == "fail"


# ---------------------------------------------------------------------------
# Article 10: Security Scan
# ---------------------------------------------------------------------------

class TestArticle10:
    """Article 10: Security Scan."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_10(str(temp_module_dir))
        assert result["result"] == "pass"

    def test_hardcoded_credential_violation(self, violation_module):
        path = violation_module(article=10)
        result = _check_article_10(path)
        assert result["result"] == "fail"
        assert any("credential" in v.get("pattern", "").lower() for v in result["violations"])

    def test_ssn_pattern_detected(self, tmp_path):
        module = tmp_path / "ssnclaw"
        module.mkdir()
        (module / "data.py").write_text('ssn = "123-45-6789"\n')
        result = _check_article_10(str(module))
        assert result["result"] == "fail"
        assert any("SSN" in v.get("pattern", "") for v in result["violations"])


# ---------------------------------------------------------------------------
# Article 11: SKILL.md Format
# ---------------------------------------------------------------------------

class TestArticle11:
    """Article 11: SKILL.md Format."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_11(str(temp_module_dir))
        assert result["result"] == "pass"

    def test_missing_skill_md_fails(self, tmp_path):
        module = tmp_path / "noskillclaw"
        module.mkdir()
        result = _check_article_11(str(module))
        assert result["result"] == "fail"

    def test_exceeding_300_lines_fails(self, violation_module):
        path = violation_module(article=11)
        result = _check_article_11(path)
        assert result["result"] == "fail"
        # Should have violations for both missing frontmatter and line count
        violation_messages = [v["message"] for v in result["violations"]]
        has_line_or_frontmatter = any(
            "300" in msg or "frontmatter" in msg for msg in violation_messages
        )
        assert has_line_or_frontmatter

    def test_missing_required_fields_fails(self, tmp_path):
        module = tmp_path / "badfmclaw"
        module.mkdir()
        (module / "SKILL.md").write_text("---\nname: badfmclaw\n---\n# test\n")
        result = _check_article_11(str(module))
        assert result["result"] == "fail"
        assert any("version" in v.get("field", "") or "description" in v.get("field", "")
                    for v in result["violations"])


# ---------------------------------------------------------------------------
# Article 12: Naming Convention
# ---------------------------------------------------------------------------

class TestArticle12:
    """Article 12: Naming Convention."""

    def test_valid_module_passes(self, temp_module_dir):
        result = _check_article_12(str(temp_module_dir), "testclaw")
        assert result["result"] == "pass"

    def test_missing_prefix_violation(self, violation_module):
        path = violation_module(article=12)
        result = _check_article_12(path, "violclaw")
        assert result["result"] == "fail"
        # Should flag add-thing (missing viol- prefix) and update_thing (not kebab-case)
        violation_actions = [v.get("action") for v in result["violations"]]
        assert "add-thing" in violation_actions or "update_thing" in violation_actions

    def test_status_action_always_allowed(self, tmp_path):
        """The 'status' action is exempt from prefix requirements."""
        module = tmp_path / "statclaw"
        module.mkdir()
        (module / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: statclaw
            version: 1.0.0
            description: Test
            ---
            # test

            | Action | Description |
            |--------|-------------|
            | `stat-add-item` | Add item |
            | `status` | Check status |
        """))
        result = _check_article_12(str(module), "statclaw")
        assert result["result"] == "pass"


# ---------------------------------------------------------------------------
# Full validate_module_static() tests
# ---------------------------------------------------------------------------

class TestValidateModuleStatic:
    """Test the full validate_module_static() function."""

    def test_valid_temp_module(self, temp_module_dir):
        result = validate_module_static(str(temp_module_dir))
        assert result["result"] == "pass"
        assert result["module_name"] == "testclaw"

    def test_result_structure(self, temp_module_dir):
        result = validate_module_static(str(temp_module_dir))
        assert "result" in result
        assert "module_name" in result
        assert "module_path" in result
        assert "articles" in result
        assert "violations" in result
        assert "skipped" in result

    def test_all_static_articles_checked(self, temp_module_dir):
        result = validate_module_static(str(temp_module_dir))
        static_nums = {a["number"] for a in get_static_articles()}
        for num in static_nums:
            assert num in result["articles"], f"Article {num} not checked"

    def test_violation_module_fails(self, violation_module):
        path = violation_module(article=2)  # Money violation
        result = validate_module_static(path)
        assert result["result"] == "fail"
        assert result["articles"][2] == "fail"


# ---------------------------------------------------------------------------
# Real module validation tests
# ---------------------------------------------------------------------------

class TestRealModules:
    """Test validate_module_static against real existing modules."""

    @pytest.mark.skipif(not os.path.isdir(LEGALCLAW_PATH), reason="legalclaw not found")
    def test_legalclaw_passes(self, src_root):
        """LegalClaw should pass all static validation."""
        result = validate_module_static(LEGALCLAW_PATH, src_root)
        # Check that critical articles pass
        for art_num in (1, 2, 3, 11):
            art_result = result["articles"].get(art_num)
            assert art_result in ("pass", "skip"), (
                f"legalclaw Article {art_num}: {art_result}, violations: "
                f"{[v for v in result['violations'] if v.get('article') == art_num]}"
            )

    @pytest.mark.skipif(not os.path.isdir(RETAILCLAW_PATH), reason="retailclaw not found")
    def test_retailclaw_passes(self, src_root):
        """RetailClaw should pass all static validation."""
        result = validate_module_static(RETAILCLAW_PATH, src_root)
        for art_num in (1, 2, 3, 11):
            art_result = result["articles"].get(art_num)
            assert art_result in ("pass", "skip"), (
                f"retailclaw Article {art_num}: {art_result}, violations: "
                f"{[v for v in result['violations'] if v.get('article') == art_num]}"
            )

    @pytest.mark.skipif(not os.path.isdir(HEALTHCLAW_VET_PATH), reason="healthclaw-vet not found")
    def test_healthclaw_vet_passes(self, src_root):
        """HealthClaw Vet should pass all static validation."""
        result = validate_module_static(HEALTHCLAW_VET_PATH, src_root)
        for art_num in (1, 2, 3, 11):
            art_result = result["articles"].get(art_num)
            assert art_result in ("pass", "skip"), (
                f"healthclaw-vet Article {art_num}: {art_result}, violations: "
                f"{[v for v in result['violations'] if v.get('article') == art_num]}"
            )


# ---------------------------------------------------------------------------
# Table Ownership Registry
# ---------------------------------------------------------------------------

class TestTableOwnershipRegistry:
    """Test build_table_ownership_registry()."""

    def test_finds_tables_from_multiple_modules(self, src_root):
        """Registry should find tables from multiple init_db.py files."""
        if not os.path.isdir(src_root):
            pytest.skip("src/ directory not found")
        registry = build_table_ownership_registry(src_root)

        # Should have a substantial number of tables
        assert len(registry) > 100, f"Only found {len(registry)} tables"

        # Check some known tables
        assert "company" in registry or "audit_log" in registry, "Core tables missing"

    def test_core_tables_owned_by_erpclaw(self, src_root):
        """Core tables should be owned by erpclaw."""
        if not os.path.isdir(src_root):
            pytest.skip("src/ directory not found")
        registry = build_table_ownership_registry(src_root)

        core_tables = ["company", "account", "gl_entry", "customer", "sales_order"]
        for table in core_tables:
            if table in registry:
                assert registry[table] == "erpclaw", (
                    f"Table '{table}' should be owned by 'erpclaw', got '{registry[table]}'"
                )

    def test_legalclaw_tables_owned_by_legalclaw(self, src_root):
        """LegalClaw tables should be owned by legalclaw."""
        if not os.path.isdir(src_root):
            pytest.skip("src/ directory not found")
        registry = build_table_ownership_registry(src_root)

        legal_tables = ["legalclaw_matter", "legalclaw_time_entry", "legalclaw_invoice"]
        for table in legal_tables:
            if table in registry:
                assert registry[table] == "legalclaw", (
                    f"Table '{table}' should be owned by 'legalclaw', got '{registry[table]}'"
                )

    def test_retailclaw_tables_owned_by_retailclaw(self, src_root):
        """RetailClaw tables should be owned by retailclaw."""
        if not os.path.isdir(src_root):
            pytest.skip("src/ directory not found")
        registry = build_table_ownership_registry(src_root)

        retail_tables = ["retailclaw_promotion", "retailclaw_coupon", "retailclaw_loyalty_program"]
        for table in retail_tables:
            if table in registry:
                assert registry[table] == "retailclaw", (
                    f"Table '{table}' should be owned by 'retailclaw', got '{registry[table]}'"
                )

    def test_registry_with_temp_modules(self, tmp_path):
        """Test registry building with temp modules."""
        src = tmp_path / "src"
        src.mkdir()

        # Create module A
        mod_a = src / "modaclaw"
        mod_a.mkdir()
        (mod_a / "init_db.py").write_text(textwrap.dedent("""\
            DDL = \"\"\"
            CREATE TABLE IF NOT EXISTS modaclaw_x (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS modaclaw_y (id TEXT PRIMARY KEY);
            \"\"\"
        """))

        # Create module B
        mod_b = src / "modbclaw"
        mod_b.mkdir()
        (mod_b / "init_db.py").write_text(textwrap.dedent("""\
            DDL = \"\"\"
            CREATE TABLE IF NOT EXISTS modbclaw_a (id TEXT PRIMARY KEY);
            \"\"\"
        """))

        registry = build_table_ownership_registry(str(src))
        assert registry.get("modaclaw_x") == "modaclaw"
        assert registry.get("modaclaw_y") == "modaclaw"
        assert registry.get("modbclaw_a") == "modbclaw"
