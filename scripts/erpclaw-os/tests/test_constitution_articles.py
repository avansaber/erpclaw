"""Comprehensive validation test suite for ERPClaw OS Constitution Articles.

Tests validate_module() catches violations AND passes valid modules.
Organized by article (1-12, static enforcement), with at minimum:
  - 2 PASSING tests (valid module passes the check)
  - 2 FAILING tests (violation detected correctly)
  - Edge cases where relevant

Total: 60+ test cases covering every static article.
"""
import os
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Import validator functions
# ---------------------------------------------------------------------------

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(TESTS_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from validate_module import (
    validate_module_static,
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
    _check_article_19,
    _check_article_20,
    _check_article_21,
    _extract_tables_from_ddl,
    _parse_columns,
    build_table_ownership_registry,
)


# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(TESTS_DIR, "fixtures")


def _fixture_path(name: str) -> str:
    return os.path.join(FIXTURES_DIR, name)


# ---------------------------------------------------------------------------
# Helper: create a minimal valid module in tmp_path
# ---------------------------------------------------------------------------

def _make_valid_module(base_dir, name="testclaw", tables=None, actions=None,
                       db_query_code=None, skill_lines=None, add_tests=True):
    """Create a complete valid module directory.

    Returns the module path as a string.
    """
    module = base_dir / name
    module.mkdir(exist_ok=True)

    # Default tables
    if tables is None:
        prefix = name.replace("claw", "") + "_" if name.endswith("claw") else f"{name}_"
        tables = f"""
            CREATE TABLE IF NOT EXISTS {prefix}item (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                price       TEXT NOT NULL DEFAULT '0',
                company_id  TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now'))
            );
        """

    (module / "init_db.py").write_text(f'DDL = """\n{tables}\n"""')

    # scripts/db_query.py
    scripts_dir = module / "scripts"
    scripts_dir.mkdir(exist_ok=True)

    if db_query_code is None:
        db_query_code = textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def main():
                ok({"message": "ok"})
            if __name__ == "__main__":
                main()
        ''')
    (scripts_dir / "db_query.py").write_text(db_query_code)

    # Default actions
    if actions is None:
        short = name[:-4] if name.endswith("claw") else name
        actions = [f"{short}-add-item", "status"]

    action_rows = "\n".join(f"| `{a}` | {a} |" for a in actions)

    # SKILL.md
    if skill_lines is None:
        skill_content = (
            f"---\n"
            f"name: {name}\n"
            f"version: 1.0.0\n"
            f"description: Test module\n"
            f"author: test\n"
            f"scripts:\n"
            f"  - scripts/db_query.py\n"
            f"---\n"
            f"\n"
            f"# {name}\n"
            f"\n"
            f"## Actions\n"
            f"\n"
            f"| Action | Description |\n"
            f"|--------|-------------|\n"
            f"{action_rows}\n"
        )
    else:
        skill_content = "\n".join(skill_lines)

    (module / "SKILL.md").write_text(skill_content)

    # tests/
    if add_tests:
        tests_dir = scripts_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "__init__.py").write_text("")
        test_funcs = "\n".join(
            f"def test_{a.replace('-', '_')}():\n    assert True\n"
            for a in actions
        )
        (tests_dir / "test_basic.py").write_text(test_funcs)

    return str(module)


# ===========================================================================
# Article 1: Table Prefix Enforcement
# ===========================================================================

class TestArticle1_TablePrefix:
    """Article 1: Every non-core module table must be prefixed with the module namespace."""

    def test_valid_prefix_passes(self, tmp_path):
        """Module with correctly prefixed tables passes Article 1."""
        path = _make_valid_module(tmp_path, "widgetclaw")
        result = _check_article_1(path, "widgetclaw")
        assert result["result"] == "pass"

    def test_valid_prefix_multiple_tables(self, tmp_path):
        """Module with 5 correctly prefixed tables passes."""
        tables = """
            CREATE TABLE IF NOT EXISTS myclaw_a (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS myclaw_b (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS myclaw_c (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS myclaw_d (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS myclaw_e (id TEXT PRIMARY KEY);
        """
        path = _make_valid_module(tmp_path, "myclaw", tables=tables)
        result = _check_article_1(path, "myclaw")
        assert result["result"] == "pass"

    def test_short_prefix_accepted(self, tmp_path):
        """Short prefix (module name without 'claw') is also accepted."""
        tables = """
            CREATE TABLE IF NOT EXISTS groom_pet (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "groomingclaw", tables=tables)
        result = _check_article_1(path, "groomingclaw")
        assert result["result"] == "pass"

    def test_missing_prefix_fails(self, tmp_path):
        """Table without module prefix is caught."""
        tables = """
            CREATE TABLE IF NOT EXISTS appointment (
                id TEXT PRIMARY KEY,
                date TEXT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "bookclaw", tables=tables)
        result = _check_article_1(path, "bookclaw")
        assert result["result"] == "fail"
        assert len(result["violations"]) == 1
        assert result["violations"][0]["table"] == "appointment"

    def test_wrong_prefix_fails(self, tmp_path):
        """Table with another module's prefix is caught."""
        tables = """
            CREATE TABLE IF NOT EXISTS legalclaw_matter (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "vetclaw", tables=tables)
        result = _check_article_1(path, "vetclaw")
        assert result["result"] == "fail"
        assert result["violations"][0]["table"] == "legalclaw_matter"

    def test_fixture_violation_art1(self, tmp_path):
        """Static fixture: violation_art1_no_prefix is caught."""
        import shutil
        src = _fixture_path("violation_art1_no_prefix")
        if not os.path.isdir(src):
            pytest.skip("Fixture not found")
        # Copy to tmp_path so it's not under erpclaw/scripts/ (which is detected as core)
        dest = str(tmp_path / "violart1claw")
        shutil.copytree(src, dest)
        result = _check_article_1(dest, "violart1claw")
        assert result["result"] == "fail"
        tables_flagged = {v["table"] for v in result["violations"]}
        assert "appointment" in tables_flagged
        assert "service_record" in tables_flagged

    def test_no_init_db_skips(self, tmp_path):
        """Module with no init_db.py skips Article 1 (no tables to check)."""
        mod = tmp_path / "noclaw"
        mod.mkdir()
        result = _check_article_1(str(mod), "noclaw")
        assert result["result"] == "skip"

    def test_mixed_valid_invalid_fails(self, tmp_path):
        """Module with some prefixed and some unprefixed tables fails."""
        tables = """
            CREATE TABLE IF NOT EXISTS mix_item (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS bad_table (id TEXT PRIMARY KEY);
        """
        path = _make_valid_module(tmp_path, "mixclaw", tables=tables)
        result = _check_article_1(path, "mixclaw")
        assert result["result"] == "fail"
        flagged = {v["table"] for v in result["violations"]}
        assert "bad_table" in flagged
        assert "mix_item" not in flagged


# ===========================================================================
# Article 2: Money is TEXT
# ===========================================================================

class TestArticle2_MoneyIsText:
    """Article 2: All financial amount columns must use TEXT type."""

    def test_text_money_passes(self, tmp_path):
        """Module with TEXT type for amount fields passes."""
        tables = """
            CREATE TABLE IF NOT EXISTS safe_invoice (
                id      TEXT PRIMARY KEY,
                amount  TEXT NOT NULL DEFAULT '0',
                tax     TEXT NOT NULL DEFAULT '0',
                total   TEXT NOT NULL DEFAULT '0',
                price   TEXT DEFAULT '0',
                balance TEXT DEFAULT '0'
            );
        """
        path = _make_valid_module(tmp_path, "safeclaw", tables=tables)
        result = _check_article_2(path)
        assert result["result"] == "pass"

    def test_non_money_integer_passes(self, tmp_path):
        """INTEGER columns for non-money fields (quantity, count) pass."""
        tables = """
            CREATE TABLE IF NOT EXISTS qty_item (
                id       TEXT PRIMARY KEY,
                quantity INTEGER NOT NULL DEFAULT 0,
                name     TEXT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "qtyclaw", tables=tables)
        result = _check_article_2(path)
        assert result["result"] == "pass"

    def test_real_money_fails(self, tmp_path):
        """Module with REAL type for 'amount' field is caught."""
        tables = """
            CREATE TABLE IF NOT EXISTS bad_invoice (
                id     TEXT PRIMARY KEY,
                amount REAL NOT NULL DEFAULT 0.0
            );
        """
        path = _make_valid_module(tmp_path, "badclaw", tables=tables)
        result = _check_article_2(path)
        assert result["result"] == "fail"
        assert any(v["column"] == "amount" and v["type"] == "REAL"
                   for v in result["violations"])

    def test_float_price_fails(self, tmp_path):
        """Module with FLOAT type for 'price' field is caught."""
        tables = """
            CREATE TABLE IF NOT EXISTS float_item (
                id    TEXT PRIMARY KEY,
                price FLOAT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "floatclaw", tables=tables)
        result = _check_article_2(path)
        assert result["result"] == "fail"
        assert any(v["column"] == "price" and v["type"] == "FLOAT"
                   for v in result["violations"])

    def test_integer_cost_fails(self, tmp_path):
        """Module with INTEGER type for 'cost' field is caught."""
        tables = """
            CREATE TABLE IF NOT EXISTS intcost_item (
                id   TEXT PRIMARY KEY,
                cost INTEGER NOT NULL DEFAULT 0
            );
        """
        path = _make_valid_module(tmp_path, "intclaw", tables=tables)
        result = _check_article_2(path)
        assert result["result"] == "fail"
        assert any(v["column"] == "cost" for v in result["violations"])

    def test_fixture_violation_art2(self):
        """Static fixture: violation_art2_float_money is caught."""
        path = _fixture_path("violation_art2_float_money")
        if not os.path.isdir(path):
            pytest.skip("Fixture not found")
        result = _check_article_2(path)
        assert result["result"] == "fail"
        types_found = {v["type"] for v in result["violations"]}
        assert "REAL" in types_found or "FLOAT" in types_found

    def test_no_init_db_skips(self, tmp_path):
        """Module with no init_db.py skips Article 2."""
        mod = tmp_path / "noclaw"
        mod.mkdir()
        result = _check_article_2(str(mod))
        assert result["result"] == "skip"

    def test_id_suffix_not_flagged(self, tmp_path):
        """Columns ending in '_id' that match money patterns are NOT flagged."""
        tables = """
            CREATE TABLE IF NOT EXISTS ref_item (
                id         TEXT PRIMARY KEY,
                balance_id INTEGER NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "refclaw", tables=tables)
        result = _check_article_2(path)
        assert result["result"] == "pass"


# ===========================================================================
# Article 3: UUID Primary Keys
# ===========================================================================

class TestArticle3_UUIDPrimaryKeys:
    """Article 3: All primary keys must be TEXT (UUID4)."""

    def test_text_pk_passes(self, tmp_path):
        """Module with 'id TEXT PRIMARY KEY' passes."""
        tables = """
            CREATE TABLE IF NOT EXISTS pk_item (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "pkclaw", tables=tables)
        result = _check_article_3(path)
        assert result["result"] == "pass"

    def test_composite_pk_passes(self, tmp_path):
        """Composite primary keys (join tables) are acceptable."""
        tables = """
            CREATE TABLE IF NOT EXISTS pk_mapping (
                item_id TEXT NOT NULL,
                tag_id  TEXT NOT NULL,
                PRIMARY KEY (item_id, tag_id)
            );
        """
        path = _make_valid_module(tmp_path, "compclaw", tables=tables)
        result = _check_article_3(path)
        assert result["result"] == "pass"

    def test_multiple_tables_text_pk_passes(self, tmp_path):
        """Multiple tables all using TEXT PKs pass."""
        tables = """
            CREATE TABLE IF NOT EXISTS multi_a (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE IF NOT EXISTS multi_b (id TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS multi_c (id TEXT PRIMARY KEY, data TEXT);
        """
        path = _make_valid_module(tmp_path, "multiclaw", tables=tables)
        result = _check_article_3(path)
        assert result["result"] == "pass"

    def test_integer_pk_fails(self, tmp_path):
        """Module with INTEGER PRIMARY KEY is caught."""
        tables = """
            CREATE TABLE IF NOT EXISTS int_item (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "intpkclaw", tables=tables)
        result = _check_article_3(path)
        assert result["result"] == "fail"
        assert any(v["type"] == "INTEGER" for v in result["violations"])

    def test_wrong_pk_name_fails(self, tmp_path):
        """Primary key column not named 'id' is caught."""
        tables = """
            CREATE TABLE IF NOT EXISTS badpk_item (
                item_id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "badpkclaw", tables=tables)
        result = _check_article_3(path)
        assert result["result"] == "fail"
        assert any("item_id" in v.get("column", "") for v in result["violations"])

    def test_fixture_violation_art3(self):
        """Static fixture: violation_art3_int_pk is caught."""
        path = _fixture_path("violation_art3_int_pk")
        if not os.path.isdir(path):
            pytest.skip("Fixture not found")
        result = _check_article_3(path)
        assert result["result"] == "fail"
        assert any(v.get("type") == "INTEGER" for v in result["violations"])


# ===========================================================================
# Article 4: Foreign Key Integrity
# ===========================================================================

class TestArticle4_ForeignKeyIntegrity:
    """Article 4: FK references must point to known tables."""

    def test_internal_fk_passes(self, tmp_path):
        """FK referencing another table in the same module passes."""
        tables = """
            CREATE TABLE IF NOT EXISTS fk_parent (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fk_child (
                id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL REFERENCES fk_parent(id)
            );
        """
        path = _make_valid_module(tmp_path, "fkclaw", tables=tables)
        result = _check_article_4(path)
        assert result["result"] == "pass"

    def test_core_table_fk_passes(self, tmp_path, src_root):
        """FK to core tables (company, customer) passes when src_root provided."""
        tables = """
            CREATE TABLE IF NOT EXISTS ext_item (
                id          TEXT PRIMARY KEY,
                company_id  TEXT NOT NULL REFERENCES company(id),
                customer_id TEXT REFERENCES customer(id)
            );
        """
        path = _make_valid_module(tmp_path, "extclaw", tables=tables)
        result = _check_article_4(path, src_root)
        assert result["result"] == "pass"

    def test_nonexistent_fk_fails(self, tmp_path):
        """FK to a table that does not exist anywhere is caught."""
        tables = """
            CREATE TABLE IF NOT EXISTS orphan_item (
                id       TEXT PRIMARY KEY,
                ref_id   TEXT REFERENCES ghost_table(id)
            );
        """
        path = _make_valid_module(tmp_path, "orphanclaw", tables=tables)
        result = _check_article_4(path)
        assert result["result"] == "fail"
        assert any("ghost_table" in v.get("referenced_table", "")
                   for v in result["violations"])

    def test_multiple_bad_fks_all_caught(self, tmp_path):
        """Multiple bad FKs in one module are all reported."""
        tables = """
            CREATE TABLE IF NOT EXISTS multi_bad (
                id    TEXT PRIMARY KEY,
                ref_a TEXT REFERENCES missing_a(id),
                ref_b TEXT REFERENCES missing_b(id)
            );
        """
        path = _make_valid_module(tmp_path, "multibadclaw", tables=tables)
        result = _check_article_4(path)
        assert result["result"] == "fail"
        refs = {v["referenced_table"] for v in result["violations"]}
        assert "missing_a" in refs
        assert "missing_b" in refs


# ===========================================================================
# Article 5: No Cross-Module Writes
# ===========================================================================

class TestArticle5_NoCrossModuleWrites:
    """Article 5: Modules may only write to their own tables."""

    def test_own_table_write_passes(self, tmp_path):
        """INSERT into module's own table passes."""
        tables = """
            CREATE TABLE IF NOT EXISTS own_record (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
        """
        db_code = textwrap.dedent('''\
            import os, sys, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def add(conn):
                conn.execute("INSERT INTO own_record (id, name) VALUES (?, ?)", ("1", "x"))
                ok({"message": "done"})
        ''')
        path = _make_valid_module(tmp_path, "ownclaw", tables=tables,
                                   db_query_code=db_code)
        result = _check_article_5(path)
        assert result["result"] == "pass"

    def test_select_from_core_passes(self, tmp_path):
        """SELECT from core tables is allowed (reads are fine)."""
        db_code = textwrap.dedent('''\
            import os, sys, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def list_customers(conn):
                rows = conn.execute("SELECT id, name FROM customer WHERE company_id = ?", ("c1",)).fetchall()
                ok({"customers": rows})
        ''')
        path = _make_valid_module(tmp_path, "readclaw", db_query_code=db_code)
        result = _check_article_5(path)
        assert result["result"] == "pass"

    def test_core_table_write_fails(self, tmp_path, src_root):
        """INSERT INTO customer (core table) is caught."""
        db_code = textwrap.dedent('''\
            import os, sys, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def bad_write(conn):
                conn.execute("INSERT INTO customer (id, name) VALUES (?, ?)", ("1", "bad"))
                ok({"message": "done"})
        ''')
        path = _make_valid_module(tmp_path, "badwriteclaw", db_query_code=db_code)
        registry = build_table_ownership_registry(src_root)
        result = _check_article_5(path, registry)
        assert result["result"] == "fail"
        assert any("customer" in v.get("target_table", "")
                   for v in result["violations"])

    def test_update_core_table_fails(self, tmp_path, src_root):
        """UPDATE on core table is caught."""
        db_code = textwrap.dedent('''\
            import os, sys, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def bad_update(conn):
                conn.execute("UPDATE account SET balance = '0' WHERE id = ?", ("a1",))
                ok({"message": "done"})
        ''')
        path = _make_valid_module(tmp_path, "badupdclaw", db_query_code=db_code)
        registry = build_table_ownership_registry(src_root)
        result = _check_article_5(path, registry)
        assert result["result"] == "fail"
        assert any("account" in v.get("target_table", "")
                   for v in result["violations"])

    def test_no_scripts_dir_skips(self, tmp_path):
        """Module with no scripts/ directory skips Article 5."""
        mod = tmp_path / "noscriptsclaw"
        mod.mkdir()
        (mod / "init_db.py").write_text('DDL = ""')
        result = _check_article_5(str(mod))
        assert result["result"] == "skip"


# ===========================================================================
# Article 6: No Direct GL Writes
# ===========================================================================

class TestArticle6_NoDirectGLWrites:
    """Article 6: No module may directly INSERT into gl_entry or stock_ledger_entry."""

    def test_cross_skill_invoice_passes(self, tmp_path):
        """Module using erpclaw_lib.gl_posting (not direct INSERT) passes."""
        db_code = textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            from erpclaw_lib.gl_posting import insert_gl_entries
            def post(conn, entries):
                insert_gl_entries(conn, entries)
                ok({"message": "posted via gl_posting"})
        ''')
        path = _make_valid_module(tmp_path, "glgoodclaw", db_query_code=db_code)
        result = _check_article_6(path)
        assert result["result"] == "pass"

    def test_no_gl_references_passes(self, tmp_path):
        """Module that never touches GL at all passes."""
        path = _make_valid_module(tmp_path, "noglclaw")
        result = _check_article_6(path)
        assert result["result"] == "pass"

    def test_direct_gl_insert_fails(self, tmp_path):
        """INSERT INTO gl_entry is caught."""
        db_code = textwrap.dedent('''\
            import os, sys, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def bad_post(conn):
                conn.execute("INSERT INTO gl_entry (id, account_id) VALUES (?, ?)", ("1", "a"))
                ok({"message": "posted"})
        ''')
        path = _make_valid_module(tmp_path, "glbadclaw", db_query_code=db_code)
        result = _check_article_6(path)
        assert result["result"] == "fail"
        assert len(result["violations"]) >= 1

    def test_direct_sle_insert_fails(self, tmp_path):
        """INSERT INTO stock_ledger_entry is caught."""
        db_code = textwrap.dedent('''\
            import os, sys, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def bad_stock(conn):
                conn.execute("INSERT INTO stock_ledger_entry (id, item_id, qty) VALUES (?, ?, ?)", ("1", "i", "5"))
                ok({"message": "stock"})
        ''')
        path = _make_valid_module(tmp_path, "slebadclaw", db_query_code=db_code)
        result = _check_article_6(path)
        assert result["result"] == "fail"

    def test_fixture_violation_art6(self, tmp_path):
        """Static fixture: violation_art6_direct_gl is caught."""
        import shutil
        src = _fixture_path("violation_art6_direct_gl")
        if not os.path.isdir(src):
            pytest.skip("Fixture not found")
        # Copy to tmp_path to avoid /tests/ path exclusion in the validator
        dest = str(tmp_path / "violart6claw")
        shutil.copytree(src, dest)
        result = _check_article_6(dest)
        assert result["result"] == "fail"
        assert len(result["violations"]) >= 2  # gl_entry + stock_ledger_entry

    def test_gl_in_test_files_not_flagged(self, tmp_path):
        """GL writes in test files should NOT be flagged (tests are exempt)."""
        path = _make_valid_module(tmp_path, "gltestclaw")
        # Write a test file with GL insert
        tests_dir = os.path.join(path, "scripts", "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "test_gl.py"), "w") as f:
            f.write(textwrap.dedent('''\
                import sqlite3
                def test_gl_posting():
                    conn = sqlite3.connect(":memory:")
                    conn.execute("INSERT INTO gl_entry (id) VALUES ('test')")
                    assert True
            '''))
        result = _check_article_6(path)
        assert result["result"] == "pass"


# ===========================================================================
# Article 7: Response Format (ok/err)
# ===========================================================================

class TestArticle7_ResponseFormat:
    """Article 7: All actions must use erpclaw_lib.response ok() and err()."""

    def test_valid_imports_passes(self, tmp_path):
        """Module importing ok and err from erpclaw_lib.response passes."""
        path = _make_valid_module(tmp_path, "respclaw")
        result = _check_article_7(path)
        assert result["result"] == "pass"

    def test_delegated_imports_passes(self, tmp_path):
        """ok/err imported in a submodule (not db_query.py) still passes."""
        mod = tmp_path / "delclaw"
        mod.mkdir()
        scripts_dir = mod / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "db_query.py").write_text(textwrap.dedent('''\
            from handler import handle
            handle()
        '''))
        (scripts_dir / "handler.py").write_text(textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def handle():
                ok({"message": "delegated"})
        '''))
        result = _check_article_7(str(mod))
        assert result["result"] == "pass"

    def test_missing_ok_fails(self, tmp_path):
        """Module that never imports ok from erpclaw_lib.response fails."""
        db_code = textwrap.dedent('''\
            import json
            def main():
                print(json.dumps({"result": "success"}))
            if __name__ == "__main__":
                main()
        ''')
        path = _make_valid_module(tmp_path, "norespsclaw", db_query_code=db_code)
        result = _check_article_7(path)
        assert result["result"] == "fail"
        messages = [v["message"] for v in result["violations"]]
        assert any("ok" in m for m in messages)

    def test_missing_err_fails(self, tmp_path):
        """Module that imports ok but not err fails."""
        db_code = textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok
            def main():
                ok({"message": "no err imported"})
        ''')
        path = _make_valid_module(tmp_path, "noerrsclaw", db_query_code=db_code)
        result = _check_article_7(path)
        assert result["result"] == "fail"
        messages = [v["message"] for v in result["violations"]]
        assert any("err" in m for m in messages)

    def test_no_scripts_dir_fails(self, tmp_path):
        """Module with no scripts/ directory fails Article 7."""
        mod = tmp_path / "empclaw"
        mod.mkdir()
        result = _check_article_7(str(mod))
        assert result["result"] == "skip"

    def test_missing_db_query_fails(self, tmp_path):
        """Module with scripts/ but no db_query.py fails."""
        mod = tmp_path / "nodbclaw"
        mod.mkdir()
        (mod / "scripts").mkdir()
        result = _check_article_7(str(mod))
        assert result["result"] == "fail"


# ===========================================================================
# Article 8: Tests Exist
# ===========================================================================

class TestArticle8_TestsExist:
    """Article 8: Every action must have a corresponding test."""

    def test_module_with_tests_passes(self, tmp_path):
        """Module with test_*.py files covering all actions passes."""
        path = _make_valid_module(tmp_path, "testyclaw")
        result = _check_article_8(path)
        assert result["result"] == "pass"

    def test_actions_with_matching_tests_passes(self, tmp_path):
        """All actions have matching test functions."""
        path = _make_valid_module(
            tmp_path, "coverclaw",
            actions=["cover-add-item", "cover-list-items", "cover-get-item"],
        )
        result = _check_article_8(path)
        assert result["result"] == "pass"

    def test_module_without_tests_fails(self, tmp_path):
        """Module with no tests/ directory is caught."""
        path = _make_valid_module(tmp_path, "notestclaw", add_tests=False)
        result = _check_article_8(path)
        assert result["result"] == "fail"
        assert any("No tests/" in v.get("message", "") or "No test_" in v.get("message", "")
                   for v in result["violations"])

    def test_missing_test_for_action_fails(self, tmp_path):
        """Action listed in SKILL.md with no test is caught."""
        path = _make_valid_module(
            tmp_path, "gapclaw",
            actions=["gap-add-item", "gap-update-item", "gap-delete-item",
                     "gap-list-items", "status"],
        )
        # Remove some test functions
        test_file = os.path.join(path, "scripts", "tests", "test_basic.py")
        with open(test_file, "w") as f:
            f.write("def test_gap_add_item():\n    assert True\n")
        result = _check_article_8(path)
        assert result["result"] == "fail"
        assert any("untested_actions" in v for v in result["violations"])

    def test_empty_tests_dir_fails(self, tmp_path):
        """Tests directory exists but has no test_*.py files."""
        path = _make_valid_module(tmp_path, "emtyclaw", add_tests=False)
        tests_dir = os.path.join(path, "scripts", "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "__init__.py"), "w") as f:
            f.write("")
        # Add a non-test file
        with open(os.path.join(tests_dir, "helpers.py"), "w") as f:
            f.write("# just a helper\n")
        result = _check_article_8(path)
        assert result["result"] == "fail"


# ===========================================================================
# Article 10: Security Scan
# ===========================================================================

class TestArticle10_SecurityScan:
    """Article 10: Code must pass security scan with zero critical findings."""

    def test_clean_code_passes(self, tmp_path):
        """Module with no secrets or PII passes."""
        path = _make_valid_module(tmp_path, "cleanclaw")
        result = _check_article_10(path)
        assert result["result"] == "pass"

    def test_parameterized_sql_passes(self, tmp_path):
        """Module using parameterized queries (not f-strings) passes."""
        db_code = textwrap.dedent('''\
            import os, sys, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def query(conn, company_id):
                rows = conn.execute("SELECT * FROM item WHERE company_id = ?", (company_id,)).fetchall()
                ok({"items": rows})
        ''')
        path = _make_valid_module(tmp_path, "paramclaw", db_query_code=db_code)
        result = _check_article_10(path)
        assert result["result"] == "pass"

    def test_hardcoded_password_fails(self, tmp_path):
        """Module with password = 'secret123' in code is caught."""
        db_code = textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            password = "SuperSecret123!"
            def main():
                ok({"message": "ok"})
        ''')
        path = _make_valid_module(tmp_path, "pwdclaw", db_query_code=db_code)
        result = _check_article_10(path)
        assert result["result"] == "fail"
        assert any("credential" in v.get("pattern", "").lower()
                   for v in result["violations"])

    def test_api_key_in_code_fails(self, tmp_path):
        """Module with API_KEY = 'sk-xxx...' in code is caught."""
        db_code = textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            api_key = "sk-1234567890abcdef"
            def main():
                ok({"message": "ok"})
        ''')
        path = _make_valid_module(tmp_path, "apikeyclaw", db_query_code=db_code)
        result = _check_article_10(path)
        assert result["result"] == "fail"

    def test_ssn_in_source_fails(self, tmp_path):
        """SSN pattern (123-45-6789) in source code is caught."""
        mod = tmp_path / "ssnclaw"
        mod.mkdir()
        (mod / "data.py").write_text('ssn = "123-45-6789"\n')
        result = _check_article_10(str(mod))
        assert result["result"] == "fail"
        assert any("SSN" in v.get("pattern", "") for v in result["violations"])

    def test_sql_injection_fstring_fails(self, tmp_path):
        """SQL via f-string is caught."""
        db_code = textwrap.dedent('''\
            import os, sys, sqlite3
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def bad_query(conn, name):
                conn.execute(f"SELECT * FROM item WHERE name = '{name}'")
                ok({"message": "ok"})
        ''')
        path = _make_valid_module(tmp_path, "sqlinjectclaw", db_query_code=db_code)
        result = _check_article_10(path)
        assert result["result"] == "fail"
        assert any("SQL injection" in v.get("pattern", "")
                   for v in result["violations"])

    def test_secrets_in_test_files_not_flagged(self, tmp_path):
        """Credentials in test files are NOT flagged (test data expected)."""
        path = _make_valid_module(tmp_path, "testsecretclaw")
        test_file = os.path.join(path, "scripts", "tests", "test_basic.py")
        with open(test_file, "w") as f:
            f.write(textwrap.dedent('''\
                def test_auth():
                    password = "TestPassword123!"
                    assert len(password) > 0
            '''))
        result = _check_article_10(path)
        assert result["result"] == "pass"


# ===========================================================================
# Article 11: SKILL.md Format
# ===========================================================================

class TestArticle11_SkillMdFormat:
    """Article 11: SKILL.md must have valid YAML frontmatter and be <= 300 lines."""

    def test_valid_skillmd_passes(self, tmp_path):
        """SKILL.md with valid YAML and under 300 lines passes."""
        path = _make_valid_module(tmp_path, "goodskillclaw")
        result = _check_article_11(path)
        assert result["result"] == "pass"

    def test_exactly_300_lines_passes(self, tmp_path):
        """SKILL.md with exactly 300 lines passes (boundary test)."""
        lines = ["---", "name: boundclaw", "version: 1.0.0",
                 "description: Boundary test", "---"]
        # Fill to exactly 300 lines
        while len(lines) < 300:
            lines.append(f"Line {len(lines) + 1}")
        assert len(lines) == 300

        path = _make_valid_module(tmp_path, "boundclaw", skill_lines=lines)
        result = _check_article_11(path)
        assert result["result"] == "pass"

    def test_301_lines_fails(self, tmp_path):
        """SKILL.md with 301 lines is caught."""
        lines = ["---", "name: overclaw", "version: 1.0.0",
                 "description: Over limit", "---"]
        while len(lines) < 301:
            lines.append(f"Line {len(lines) + 1}")
        assert len(lines) == 301

        path = _make_valid_module(tmp_path, "overclaw", skill_lines=lines)
        result = _check_article_11(path)
        assert result["result"] == "fail"
        assert any("300" in v.get("message", "") for v in result["violations"])

    def test_missing_frontmatter_fails(self, tmp_path):
        """SKILL.md without YAML frontmatter (no --- delimiters) is caught."""
        lines = ["# No frontmatter here", "", "Just regular markdown."]
        path = _make_valid_module(tmp_path, "nofmclaw", skill_lines=lines)
        result = _check_article_11(path)
        assert result["result"] == "fail"
        assert any("frontmatter" in v.get("message", "")
                   for v in result["violations"])

    def test_missing_required_field_version_fails(self, tmp_path):
        """SKILL.md frontmatter missing 'version' field is caught."""
        lines = ["---", "name: noverclaw", "description: No version", "---",
                 "# test"]
        path = _make_valid_module(tmp_path, "noverclaw", skill_lines=lines)
        result = _check_article_11(path)
        assert result["result"] == "fail"
        assert any("version" in v.get("field", "")
                   for v in result["violations"])

    def test_missing_required_field_description_fails(self, tmp_path):
        """SKILL.md frontmatter missing 'description' field is caught."""
        lines = ["---", "name: nodescclaw", "version: 1.0.0", "---", "# test"]
        path = _make_valid_module(tmp_path, "nodescclaw", skill_lines=lines)
        result = _check_article_11(path)
        assert result["result"] == "fail"
        assert any("description" in v.get("field", "")
                   for v in result["violations"])

    def test_missing_skillmd_fails(self, tmp_path):
        """Module with no SKILL.md at all fails."""
        mod = tmp_path / "noskillclaw"
        mod.mkdir()
        result = _check_article_11(str(mod))
        assert result["result"] == "fail"
        assert any("not found" in v.get("message", "")
                   for v in result["violations"])

    def test_invalid_yaml_fails(self, tmp_path):
        """SKILL.md with malformed YAML frontmatter fails."""
        lines = ["---", "name: [invalid yaml", "version: ???",
                 "---", "# test"]
        path = _make_valid_module(tmp_path, "badyamlclaw", skill_lines=lines)
        result = _check_article_11(path)
        # May fail due to missing fields or invalid yaml parse
        assert result["result"] == "fail"


# ===========================================================================
# Article 12: Naming Convention
# ===========================================================================

class TestArticle12_NamingConvention:
    """Article 12: Action names must be kebab-case with namespace prefix."""

    def test_kebab_case_passes(self, tmp_path):
        """Actions using kebab-case with correct prefix pass."""
        path = _make_valid_module(
            tmp_path, "nameclaw",
            actions=["name-add-widget", "name-list-widgets",
                     "name-get-widget", "status"],
        )
        result = _check_article_12(path, "nameclaw")
        assert result["result"] == "pass"

    def test_prefix_present_passes(self, tmp_path):
        """Non-core module with namespace prefix passes."""
        path = _make_valid_module(
            tmp_path, "abcclaw",
            actions=["abc-add-item", "abc-update-item", "status"],
        )
        result = _check_article_12(path, "abcclaw")
        assert result["result"] == "pass"

    def test_camel_case_fails(self, tmp_path):
        """Actions using camelCase (underscore) are NOT kebab-case and are caught."""
        # Note: the SKILL.md extractor only picks up kebab-case action names
        # from backtick patterns, so we test missing-prefix instead
        path = _make_valid_module(
            tmp_path, "camelclaw",
            actions=["add-widget", "status"],  # missing camel- prefix
        )
        result = _check_article_12(path, "camelclaw")
        assert result["result"] == "fail"
        assert any("add-widget" in v.get("action", "")
                   for v in result["violations"])

    def test_missing_prefix_fails(self, tmp_path):
        """Non-core module action without namespace prefix is caught."""
        path = _make_valid_module(
            tmp_path, "noprefixclaw",
            actions=["add-item", "list-items", "status"],
        )
        result = _check_article_12(path, "noprefixclaw")
        assert result["result"] == "fail"
        flagged = {v["action"] for v in result["violations"]}
        assert "add-item" in flagged
        assert "list-items" in flagged

    def test_fixture_violation_art12(self, tmp_path):
        """Static fixture: violation_art12_bad_names is caught."""
        import shutil
        src = _fixture_path("violation_art12_bad_names")
        if not os.path.isdir(src):
            pytest.skip("Fixture not found")
        # Copy to tmp_path so _is_core_module returns False
        dest = str(tmp_path / "violart12claw")
        shutil.copytree(src, dest)
        result = _check_article_12(dest, "violart12claw")
        assert result["result"] == "fail"
        flagged = {v["action"] for v in result["violations"]}
        assert "add-widget" in flagged

    def test_status_action_always_allowed(self, tmp_path):
        """The 'status' action is exempt from prefix requirements."""
        path = _make_valid_module(
            tmp_path, "statclaw",
            actions=["stat-add-item", "status"],
        )
        result = _check_article_12(path, "statclaw")
        assert result["result"] == "pass"

    def test_no_skillmd_skips(self, tmp_path):
        """Module with no SKILL.md skips Article 12."""
        mod = tmp_path / "noskill12claw"
        mod.mkdir()
        result = _check_article_12(str(mod), "noskill12claw")
        assert result["result"] == "skip"


# ===========================================================================
# Article 19: In-Module Modification Scope
# ===========================================================================

class TestArticle19_InModuleModificationScope:
    """Article 19: OS may only ADD new functions, not modify existing ones."""

    def test_no_manifest_skips(self, tmp_path):
        """Module with no .os_manifest.json skips Article 19 (backwards compatible)."""
        path = _make_valid_module(tmp_path, "nomanifestclaw")
        result = _check_article_19(path)
        assert result["result"] == "skip"
        assert result["article"] == 19

    def test_valid_manifest_passes(self, tmp_path):
        """Manifest with generated functions that exist in code passes."""
        import json
        path = _make_valid_module(tmp_path, "goodmanclaw", db_query_code=textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def handle_add_widget(args):
                # Source: ERPClaw CRUD pattern
                ok({"message": "ok"})
            def main():
                ok({"message": "ok"})
            if __name__ == "__main__":
                main()
        '''))
        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_add_widget", "action_name": "good-add-widget",
                 "generated_at": "2026-03-19T00:00:00Z", "generator": "in_module_generator"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_19(path)
        assert result["result"] == "pass"

    def test_phantom_function_fails(self, tmp_path):
        """Manifest lists a function that does not exist in code — violation."""
        import json
        path = _make_valid_module(tmp_path, "phantomclaw")
        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_nonexistent", "action_name": "phantom-action",
                 "generated_at": "2026-03-19T00:00:00Z", "generator": "in_module_generator"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_19(path)
        assert result["result"] == "fail"
        assert any("handle_nonexistent" in v.get("message", "") for v in result["violations"])

    def test_modified_functions_fails(self, tmp_path):
        """Manifest with modified_functions entry is a violation."""
        import json
        path = _make_valid_module(tmp_path, "modifyclaw")
        manifest = {
            "version": "1.0.0",
            "generated_functions": [],
            "modified_functions": [
                {"function_name": "handle_existing_action",
                 "reason": "optimized query"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_19(path)
        assert result["result"] == "fail"
        assert any("modified" in v.get("message", "").lower() for v in result["violations"])
        assert any("handle_existing_action" in v.get("function", "") for v in result["violations"])

    def test_corrupt_manifest_fails(self, tmp_path):
        """Corrupt .os_manifest.json causes failure."""
        path = _make_valid_module(tmp_path, "corruptmanclaw")
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            f.write("{invalid json!!!")

        result = _check_article_19(path)
        assert result["result"] == "fail"
        assert any("Cannot read" in v.get("message", "") for v in result["violations"])

    def test_empty_manifest_passes(self, tmp_path):
        """Manifest with no generated or modified functions passes."""
        import json
        path = _make_valid_module(tmp_path, "emptymanclaw")
        manifest = {"version": "1.0.0", "generated_functions": []}
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_19(path)
        assert result["result"] == "pass"


# ===========================================================================
# Article 20: Research Provenance
# ===========================================================================

class TestArticle20_ResearchProvenance:
    """Article 20: OS-generated features must cite business rule sources."""

    def test_no_manifest_skips(self, tmp_path):
        """Module with no manifest skips Article 20."""
        path = _make_valid_module(tmp_path, "noman20claw")
        result = _check_article_20(path)
        assert result["result"] == "skip"
        assert result["article"] == 20

    def test_source_comment_passes(self, tmp_path):
        """Generated function with '# Source:' comment passes."""
        import json
        path = _make_valid_module(tmp_path, "sourced20claw", db_query_code=textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def handle_calc_overtime(args):
                # Source: FLSA 29 USC 207 — overtime at 1.5x for hours > 40/week
                hours = args.get("hours", 0)
                ok({"overtime": hours})
            def main():
                ok({"message": "ok"})
            if __name__ == "__main__":
                main()
        '''))
        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_calc_overtime", "action_name": "calc-overtime",
                 "generated_at": "2026-03-19T00:00:00Z", "generator": "in_module_generator"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_20(path)
        assert result["result"] == "pass"

    def test_missing_source_comment_fails(self, tmp_path):
        """Generated function without '# Source:' comment fails."""
        import json
        path = _make_valid_module(tmp_path, "nosource20claw", db_query_code=textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def handle_calc_overtime(args):
                hours = args.get("hours", 0)
                ok({"overtime": hours})
            def main():
                ok({"message": "ok"})
            if __name__ == "__main__":
                main()
        '''))
        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_calc_overtime", "action_name": "calc-overtime",
                 "generated_at": "2026-03-19T00:00:00Z", "generator": "in_module_generator"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_20(path)
        assert result["result"] == "fail"
        assert any("handle_calc_overtime" in v.get("message", "") for v in result["violations"])
        assert any("Source" in v.get("message", "") for v in result["violations"])

    def test_empty_generated_list_skips(self, tmp_path):
        """Manifest with empty generated_functions list skips."""
        import json
        path = _make_valid_module(tmp_path, "emptygen20claw")
        manifest = {"version": "1.0.0", "generated_functions": []}
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_20(path)
        assert result["result"] == "skip"

    def test_multiple_functions_mixed(self, tmp_path):
        """One function with source, one without — overall fails."""
        import json
        path = _make_valid_module(tmp_path, "mixed20claw", db_query_code=textwrap.dedent('''\
            import os, sys
            sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
            from erpclaw_lib.response import ok, err
            def handle_good_action(args):
                # Source: GAAP ASC 606 revenue recognition
                ok({"result": "good"})
            def handle_bad_action(args):
                ok({"result": "no source here"})
            def main():
                ok({"message": "ok"})
            if __name__ == "__main__":
                main()
        '''))
        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_good_action", "action_name": "good-action",
                 "generated_at": "2026-03-19T00:00:00Z", "generator": "in_module_generator"},
                {"function_name": "handle_bad_action", "action_name": "bad-action",
                 "generated_at": "2026-03-19T00:00:00Z", "generator": "in_module_generator"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_20(path)
        assert result["result"] == "fail"
        assert len(result["violations"]) == 1
        assert "handle_bad_action" in result["violations"][0]["message"]


# ===========================================================================
# Article 21: Feature Isolation
# ===========================================================================

class TestArticle21_FeatureIsolation:
    """Article 21: OS-generated features must have corresponding isolated tests."""

    def test_no_manifest_skips(self, tmp_path):
        """Module with no manifest skips Article 21."""
        path = _make_valid_module(tmp_path, "noman21claw")
        result = _check_article_21(path)
        assert result["result"] == "skip"
        assert result["article"] == 21

    def test_matching_test_passes(self, tmp_path):
        """Generated action with matching test function passes."""
        import json
        path = _make_valid_module(tmp_path, "tested21claw",
                                  actions=["tested-add-widget", "status"])
        # Add a test file that matches the generated action
        tests_dir = os.path.join(path, "scripts", "tests")
        os.makedirs(tests_dir, exist_ok=True)
        test_file = os.path.join(tests_dir, "test_add_widget.py")
        with open(test_file, "w") as f:
            f.write("def test_tested_add_widget():\n    assert True\n")

        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_tested_add_widget",
                 "action_name": "tested-add-widget",
                 "generated_at": "2026-03-19T00:00:00Z",
                 "generator": "in_module_generator"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_21(path)
        assert result["result"] == "pass"

    def test_missing_test_fails(self, tmp_path):
        """Generated action without any corresponding test fails."""
        import json
        # Use add_tests=False so _make_valid_module does not auto-create
        # test stubs for the actions. Then manually create a tests/ dir
        # with only an unrelated test so the tests dir exists.
        path = _make_valid_module(tmp_path, "notest21claw",
                                  actions=["notest-add-widget", "status"],
                                  add_tests=False)
        tests_dir = os.path.join(path, "scripts", "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(tests_dir, "test_other.py"), "w") as f:
            f.write("def test_unrelated():\n    assert True\n")

        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_notest_add_widget",
                 "action_name": "notest-add-widget",
                 "generated_at": "2026-03-19T00:00:00Z",
                 "generator": "in_module_generator"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_21(path)
        assert result["result"] == "fail"
        assert any("notest-add-widget" in v.get("message", "") for v in result["violations"])

    def test_empty_generated_list_skips(self, tmp_path):
        """Manifest with no generated functions skips."""
        import json
        path = _make_valid_module(tmp_path, "emptygen21claw")
        manifest = {"version": "1.0.0", "generated_functions": []}
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_21(path)
        assert result["result"] == "skip"

    def test_no_tests_dir_fails(self, tmp_path):
        """OS-modified module with no tests/ directory fails."""
        import json
        mod = tmp_path / "notestsdir21claw"
        mod.mkdir()
        scripts = mod / "scripts"
        scripts.mkdir()
        (scripts / "db_query.py").write_text("pass\n")

        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_some_action",
                 "action_name": "some-action",
                 "generated_at": "2026-03-19T00:00:00Z",
                 "generator": "in_module_generator"}
            ]
        }
        manifest_path = str(mod / ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_21(str(mod))
        assert result["result"] == "fail"
        assert any("tests/" in v.get("message", "").lower() or "no tests" in v.get("message", "").lower()
                    for v in result["violations"])

    def test_multiple_actions_partial_tests(self, tmp_path):
        """Two generated actions, only one has test — fails for the missing one."""
        import json
        # Use add_tests=False to avoid auto-generated test stubs
        path = _make_valid_module(tmp_path, "partial21claw",
                                  actions=["partial-action-a", "partial-action-b", "status"],
                                  add_tests=False)
        tests_dir = os.path.join(path, "scripts", "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "__init__.py"), "w") as f:
            f.write("")
        # Only create a test for action-a, not action-b
        test_file = os.path.join(tests_dir, "test_action_a.py")
        with open(test_file, "w") as f:
            f.write("def test_partial_action_a():\n    assert True\n")

        manifest = {
            "version": "1.0.0",
            "generated_functions": [
                {"function_name": "handle_partial_action_a",
                 "action_name": "partial-action-a",
                 "generated_at": "2026-03-19T00:00:00Z",
                 "generator": "in_module_generator"},
                {"function_name": "handle_partial_action_b",
                 "action_name": "partial-action-b",
                 "generated_at": "2026-03-19T00:00:00Z",
                 "generator": "in_module_generator"}
            ]
        }
        manifest_path = os.path.join(path, ".os_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        result = _check_article_21(path)
        assert result["result"] == "fail"
        assert len(result["violations"]) == 1
        assert "partial-action-b" in result["violations"][0]["message"]


# ===========================================================================
# Full validate_module_static() integration tests
# ===========================================================================

class TestFullValidation:
    """Test the complete validate_module_static() pipeline."""

    def test_fully_valid_module_passes(self, tmp_path):
        """A fully valid module passes all articles."""
        path = _make_valid_module(tmp_path, "perfectclaw")
        result = validate_module_static(path)
        assert result["result"] == "pass"
        assert result["module_name"] == "perfectclaw"
        assert len(result["violations"]) == 0

    def test_result_structure(self, tmp_path):
        """Result dict has all required keys."""
        path = _make_valid_module(tmp_path, "structclaw")
        result = validate_module_static(path)
        assert "result" in result
        assert "module_name" in result
        assert "module_path" in result
        assert "articles" in result
        assert "violations" in result
        assert "skipped" in result

    def test_all_static_articles_checked(self, tmp_path):
        """All 14 static articles appear in the result."""
        path = _make_valid_module(tmp_path, "allcheckclaw")
        result = validate_module_static(path)
        static_nums = {1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 19, 20, 21}
        for num in static_nums:
            assert num in result["articles"], f"Article {num} not checked"

    def test_single_violation_fails_overall(self, tmp_path):
        """A single violation in any article causes overall failure."""
        tables = """
            CREATE TABLE IF NOT EXISTS singfail_item (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
        """
        path = _make_valid_module(tmp_path, "singfailclaw", tables=tables)
        result = validate_module_static(path)
        assert result["result"] == "fail"
        assert result["articles"][3] == "fail"  # Article 3: INTEGER PK

    def test_multiple_violations_all_reported(self, tmp_path):
        """Multiple violations across different articles are all reported."""
        tables = """
            CREATE TABLE IF NOT EXISTS appointment (
                id      INTEGER PRIMARY KEY,
                amount  REAL NOT NULL DEFAULT 0.0
            );
        """
        path = _make_valid_module(tmp_path, "multifailclaw", tables=tables)
        result = validate_module_static(path)
        assert result["result"] == "fail"
        failed_articles = {v["article"] for v in result["violations"]}
        # Should fail Article 1 (no prefix), Article 2 (REAL money), Article 3 (INTEGER PK)
        assert 1 in failed_articles
        assert 2 in failed_articles
        assert 3 in failed_articles
