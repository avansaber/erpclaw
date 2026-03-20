"""Tests for ERPClaw OS in_module_generator — in-module code insertion.

Covers:
- analyze_module: parsing real selling/buying db_query.py
- generate_feature_code: valid Python output, module pattern matching
- insert_feature: backup creation, ACTIONS dict extension
- validate_insertion: syntax checking, regression detection
- is_safe_to_modify: safety exclusion enforcement
- Full cycle: create temp module, insert feature, validate
"""
import ast
import json
import os
import re
import sys
import textwrap

import pytest

# Make the erpclaw-os package importable
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(TESTS_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from in_module_generator import (
    analyze_module,
    generate_feature_code,
    insert_feature,
    validate_insertion,
    is_safe_to_modify,
    _action_to_func_name,
    _extract_verb,
    _find_actions_dict,
    _validate_syntax,
)
from dgm_engine import SAFETY_EXCLUDED_FILES

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# tests/ -> erpclaw-os/ -> scripts/ -> erpclaw/ -> src/ -> project-root/
_PROJECT_ROOT = OS_DIR
for _ in range(4):
    _PROJECT_ROOT = os.path.dirname(_PROJECT_ROOT)
SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")

SELLING_DB_QUERY = os.path.join(
    SRC_ROOT, "erpclaw", "scripts", "erpclaw-selling", "db_query.py"
)
BUYING_DB_QUERY = os.path.join(
    SRC_ROOT, "erpclaw", "scripts", "erpclaw-buying", "db_query.py"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db_query(tmp_path):
    """Create a temporary db_query.py with a standard structure."""
    module_dir = tmp_path / "testmod" / "scripts"
    module_dir.mkdir(parents=True)
    db_query = module_dir / "db_query.py"
    db_query.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os
        import sys
        import json
        import uuid
        from datetime import datetime, timezone
        from decimal import Decimal, InvalidOperation

        sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
        from erpclaw_lib.response import ok, err
        from erpclaw_lib.query import Q, P, Table, Field, dynamic_update


        def add_widget(args):
            \"\"\"Add a new widget.\"\"\"
            db_path = getattr(args, 'db_path', None)
            name = getattr(args, 'name', None)
            if not name:
                err("--name is required")
            ok({"id": "test-id", "message": "Widget added"})


        def list_widgets(args):
            \"\"\"List all widgets.\"\"\"
            ok({"rows": [], "count": 0})


        ACTIONS = {
            "test-add-widget": add_widget,
            "test-list-widgets": list_widgets,
        }


        def main():
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
            parser.add_argument("--db-path", default=None)
            parser.add_argument("--name", default=None)
            args = parser.parse_args()
            handler = ACTIONS[args.action]
            handler(args)


        if __name__ == "__main__":
            main()
    """))
    return str(db_query)


@pytest.fixture
def temp_module_dir(tmp_path):
    """Create a temporary module directory with db_query.py inside scripts/."""
    module_dir = tmp_path / "tempclaw"
    scripts_dir = module_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    db_query = scripts_dir / "db_query.py"
    db_query.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os
        import sys
        import json

        sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
        from erpclaw_lib.response import ok, err
        from erpclaw_lib.query import Q, P, Table, Field, dynamic_update


        def handle_status(args):
            \"\"\"Check module status.\"\"\"
            ok({"message": "tempclaw is running"})


        ACTIONS = {
            "temp-status": handle_status,
        }


        def main():
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--action", required=True)
            args = parser.parse_args()
            handler = ACTIONS.get(args.action)
            if handler:
                handler(args)
            else:
                err(f"Unknown action: {args.action}")


        if __name__ == "__main__":
            main()
    """))
    return str(module_dir)


# ===========================================================================
# Test: analyze_module — parsing real selling db_query.py
# ===========================================================================

class TestAnalyzeModuleSelling:
    """Test analyze_module against the real selling db_query.py."""

    @pytest.mark.skipif(
        not os.path.isfile(SELLING_DB_QUERY),
        reason="Selling db_query.py not found at expected path",
    )
    def test_analyze_module_parses_selling(self):
        result = analyze_module(SELLING_DB_QUERY)
        assert "error" not in result, f"Analysis failed: {result.get('error')}"
        assert result["file_path"] == SELLING_DB_QUERY
        assert result["line_count"] > 100
        assert result["actions_dict_line"] is not None
        assert result["uses_ok_err"] is True
        assert result["uses_pypika"] is True
        assert result["uses_decimal"] is True
        assert result["argparser_line"] is not None

    @pytest.mark.skipif(
        not os.path.isfile(SELLING_DB_QUERY),
        reason="Selling db_query.py not found at expected path",
    )
    def test_analyze_module_finds_selling_actions(self):
        result = analyze_module(SELLING_DB_QUERY)
        actions = result["actions"]
        # Selling must have these known actions
        assert "add-customer" in actions
        assert "add-sales-order" in actions
        assert "submit-sales-order" in actions
        assert "create-delivery-note" in actions
        assert "create-sales-invoice" in actions

    @pytest.mark.skipif(
        not os.path.isfile(SELLING_DB_QUERY),
        reason="Selling db_query.py not found at expected path",
    )
    def test_analyze_module_selling_action_count(self):
        result = analyze_module(SELLING_DB_QUERY)
        # Selling has 35+ actions
        assert len(result["actions"]) >= 30


# ===========================================================================
# Test: analyze_module — parsing real buying db_query.py
# ===========================================================================

class TestAnalyzeModuleBuying:
    """Test analyze_module against the real buying db_query.py."""

    @pytest.mark.skipif(
        not os.path.isfile(BUYING_DB_QUERY),
        reason="Buying db_query.py not found at expected path",
    )
    def test_analyze_module_parses_buying(self):
        result = analyze_module(BUYING_DB_QUERY)
        assert "error" not in result, f"Analysis failed: {result.get('error')}"
        assert result["file_path"] == BUYING_DB_QUERY
        assert result["line_count"] > 100
        assert result["actions_dict_line"] is not None
        assert result["uses_ok_err"] is True
        assert result["uses_pypika"] is True

    @pytest.mark.skipif(
        not os.path.isfile(BUYING_DB_QUERY),
        reason="Buying db_query.py not found at expected path",
    )
    def test_analyze_module_buying_actions(self):
        result = analyze_module(BUYING_DB_QUERY)
        actions = result["actions"]
        assert "add-supplier" in actions
        assert "add-purchase-order" in actions
        assert "submit-purchase-order" in actions


# ===========================================================================
# Test: analyze_module — ACTIONS dict detection
# ===========================================================================

class TestAnalyzeModuleActionsDict:
    """Test that analyze_module correctly finds and parses the ACTIONS dict."""

    def test_finds_actions_dict_in_temp(self, temp_db_query):
        result = analyze_module(temp_db_query)
        assert "error" not in result
        assert result["actions_dict_line"] is not None
        assert result["actions_dict_end_line"] is not None

    def test_counts_existing_actions(self, temp_db_query):
        result = analyze_module(temp_db_query)
        assert len(result["actions"]) == 2
        assert "test-add-widget" in result["actions"]
        assert "test-list-widgets" in result["actions"]

    def test_detects_function_names_in_dict(self, temp_db_query):
        result = analyze_module(temp_db_query)
        assert result["actions"]["test-add-widget"] == "add_widget"
        assert result["actions"]["test-list-widgets"] == "list_widgets"

    def test_detects_functions_list(self, temp_db_query):
        result = analyze_module(temp_db_query)
        func_names = [f["name"] for f in result["functions"]]
        assert "add_widget" in func_names
        assert "list_widgets" in func_names
        assert "main" in func_names

    def test_detects_imports(self, temp_db_query):
        result = analyze_module(temp_db_query)
        assert result["uses_ok_err"] is True
        assert result["uses_pypika"] is True
        assert result["uses_decimal"] is True
        assert result["imports_end_line"] > 0


# ===========================================================================
# Test: generate_feature_code
# ===========================================================================

class TestGenerateFeatureCode:
    """Test that generate_feature_code produces valid, pattern-following Python."""

    def test_produces_valid_python(self, temp_db_query):
        analysis = analyze_module(temp_db_query)
        spec = {
            "action_name": "test-get-widget",
            "parameters": [
                {"name": "widget-id", "type": "str", "required": True, "description": "Widget ID"},
            ],
            "description": "Get a widget by ID",
            "table_name": "test_widget",
        }
        code = generate_feature_code(spec, analysis)

        # Must be valid Python
        try:
            ast.parse(code)
        except SyntaxError as e:
            pytest.fail(f"Generated code has syntax error: {e}\n\nCode:\n{code}")

    def test_uses_module_patterns_ok_err(self, temp_db_query):
        analysis = analyze_module(temp_db_query)
        spec = {
            "action_name": "test-delete-widget",
            "parameters": [],
            "description": "Delete a widget",
        }
        code = generate_feature_code(spec, analysis)
        # Module uses ok/err, so generated code should too
        assert "ok(" in code

    def test_uses_module_patterns_pypika(self, temp_db_query):
        analysis = analyze_module(temp_db_query)
        spec = {
            "action_name": "test-list-gadgets",
            "parameters": [],
            "description": "List gadgets",
            "table_name": "test_gadget",
        }
        code = generate_feature_code(spec, analysis)
        # Module uses PyPika, so generated code should reference Table()
        assert "Table(" in code

    def test_includes_decimal_for_financial(self, temp_db_query):
        analysis = analyze_module(temp_db_query)
        spec = {
            "action_name": "test-add-payment",
            "parameters": [
                {"name": "amount", "type": "decimal", "required": True,
                 "description": "Payment amount", "is_financial": True},
                {"name": "company-id", "type": "str", "required": True,
                 "description": "Company ID"},
            ],
            "description": "Record a payment",
            "table_name": "test_payment",
            "is_financial": True,
        }
        code = generate_feature_code(spec, analysis)
        # Financial actions must have Decimal handling
        assert "Decimal" in code

    def test_function_name_matches_action(self, temp_db_query):
        analysis = analyze_module(temp_db_query)
        spec = {
            "action_name": "test-archive-widget",
            "parameters": [],
            "description": "Archive a widget",
        }
        code = generate_feature_code(spec, analysis)
        assert "def test_archive_widget(args):" in code

    def test_generates_docstring(self, temp_db_query):
        analysis = analyze_module(temp_db_query)
        spec = {
            "action_name": "test-reopen-widget",
            "parameters": [],
            "description": "Reopen a closed widget",
        }
        code = generate_feature_code(spec, analysis)
        assert '"""Reopen a closed widget' in code


# ===========================================================================
# Test: insert_feature
# ===========================================================================

class TestInsertFeature:
    """Test that insert_feature correctly modifies db_query.py files."""

    def test_creates_backup(self, temp_db_query):
        code = textwrap.dedent("""\
        def test_new_func(args):
            \"\"\"A new function.\"\"\"
            pass
        """)
        result = insert_feature(temp_db_query, code, "test-new-func")
        assert result["success"] is True
        assert result["backup_path"].endswith(".bak")
        assert os.path.isfile(result["backup_path"])

    def test_adds_to_actions_dict(self, temp_db_query):
        code = textwrap.dedent("""\
        def test_frobnicate(args):
            \"\"\"Frobnicate the widget.\"\"\"
            pass
        """)
        result = insert_feature(temp_db_query, code, "test-frobnicate")
        assert result["success"] is True
        assert result["action_added"] == "test-frobnicate"

        # Verify the action is in the file
        with open(temp_db_query, "r") as f:
            source = f.read()
        assert '"test-frobnicate": test_frobnicate,' in source

    def test_validates_syntax_after_insert(self, temp_db_query):
        code = textwrap.dedent("""\
        def valid_func(args):
            \"\"\"Valid function.\"\"\"
            x = 1 + 2
        """)
        result = insert_feature(temp_db_query, code, "test-valid-func")
        assert result["success"] is True

        # The modified file must be valid Python
        with open(temp_db_query, "r") as f:
            source = f.read()
        try:
            compile(source, temp_db_query, "exec")
        except SyntaxError as e:
            pytest.fail(f"Inserted code produced invalid syntax: {e}")

    def test_rejects_duplicate_action(self, temp_db_query):
        code = "def add_widget(args):\n    pass\n"
        result = insert_feature(temp_db_query, code, "test-add-widget")
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_rejects_duplicate_function(self, temp_db_query):
        code = "def add_widget(args):\n    pass\n"
        result = insert_feature(temp_db_query, code, "test-new-action")
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_lines_added_reported(self, temp_db_query):
        code = textwrap.dedent("""\
        def count_things(args):
            \"\"\"Count things.\"\"\"
            x = 1
            y = 2
            return x + y
        """)
        result = insert_feature(temp_db_query, code, "test-count-things")
        assert result["success"] is True
        assert result["lines_added"] > 0

    def test_writes_os_manifest(self, temp_db_query):
        code = textwrap.dedent("""\
        def do_something(args):
            \"\"\"Do something.\"\"\"
            pass
        """)
        result = insert_feature(temp_db_query, code, "test-do-something")
        assert result["success"] is True

        manifest_path = os.path.join(os.path.dirname(temp_db_query), ".os_manifest.json")
        assert os.path.isfile(manifest_path)

        with open(manifest_path, "r") as f:
            manifest = json.loads(f.read())
        assert len(manifest["generated_functions"]) == 1
        assert manifest["generated_functions"][0]["action_name"] == "test-do-something"
        assert manifest["generated_functions"][0]["function_name"] == "do_something"

    def test_rejects_syntax_error_code(self, temp_db_query):
        bad_code = "def broken_func(args)\n    pass\n"  # Missing colon
        result = insert_feature(temp_db_query, bad_code, "test-broken")
        assert result["success"] is False
        assert "syntax error" in result["error"].lower() or "Syntax" in result["error"]

        # Original file should be unchanged (restored from backup)
        with open(temp_db_query, "r") as f:
            source = f.read()
        assert "broken_func" not in source


# ===========================================================================
# Test: is_safe_to_modify
# ===========================================================================

class TestIsSafeToModify:
    """Test safety exclusion enforcement."""

    def test_blocks_safety_excluded_files(self, tmp_path):
        """Files in SAFETY_EXCLUDED_FILES must be rejected."""
        for excluded_name in ["gl_posting.py", "constitution.py", "dgm_engine.py",
                              "in_module_generator.py"]:
            excluded_file = tmp_path / excluded_name
            excluded_file.write_text("# safety excluded file\n")
            safe, reason = is_safe_to_modify(str(excluded_file))
            assert safe is False, f"{excluded_name} should be blocked but was allowed"
            assert "SAFETY_EXCLUDED_FILES" in reason or "safety" in reason.lower()

    def test_allows_normal_files(self, temp_db_query):
        """Normal db_query.py files should be allowed."""
        safe, reason = is_safe_to_modify(temp_db_query)
        assert safe is True, f"Normal file should be allowed but got: {reason}"

    def test_blocks_nonexistent_file(self, tmp_path):
        safe, reason = is_safe_to_modify(str(tmp_path / "nonexistent.py"))
        assert safe is False


# ===========================================================================
# Test: validate_insertion
# ===========================================================================

class TestValidateInsertion:
    """Test post-insertion validation."""

    def test_catches_syntax_error(self, tmp_path):
        """A file with invalid syntax should fail validation."""
        bad_file = tmp_path / "db_query.py"
        bad_file.write_text("def broken(:\n    pass\n")
        result = validate_insertion(str(bad_file), "some-action")
        assert result["valid"] is False
        syntax_check = next(c for c in result["checks"] if c["name"] == "syntax_valid")
        assert syntax_check["passed"] is False

    def test_validates_good_insertion(self, temp_db_query):
        """After a successful insertion, validate_insertion should pass."""
        code = textwrap.dedent("""\
        def test_ping(args):
            \"\"\"Ping the module.\"\"\"
            pass
        """)
        insert_result = insert_feature(temp_db_query, code, "test-ping")
        assert insert_result["success"] is True

        val_result = validate_insertion(temp_db_query, "test-ping")
        # Syntax and action/function checks must pass
        syntax_check = next(c for c in val_result["checks"] if c["name"] == "syntax_valid")
        assert syntax_check["passed"] is True

        action_check = next(c for c in val_result["checks"] if c["name"] == "action_in_dict")
        assert action_check["passed"] is True

        func_check = next(c for c in val_result["checks"] if c["name"] == "function_exists")
        assert func_check["passed"] is True

    def test_detects_missing_action(self, temp_db_query):
        """If action is not in ACTIONS dict, validation should report it."""
        result = validate_insertion(temp_db_query, "nonexistent-action")
        action_check = next(c for c in result["checks"] if c["name"] == "action_in_dict")
        assert action_check["passed"] is False

    def test_detects_missing_function(self, temp_db_query):
        """If function is not defined, validation should report it."""
        result = validate_insertion(temp_db_query, "nonexistent-action")
        func_check = next(c for c in result["checks"] if c["name"] == "function_exists")
        assert func_check["passed"] is False


# ===========================================================================
# Test: full cycle on temp module
# ===========================================================================

class TestFullCycle:
    """End-to-end test: analyze -> generate -> insert -> validate on a temp module."""

    def test_full_cycle_on_temp_module(self, temp_module_dir):
        """Create a temp module, insert a feature, validate the result."""
        db_query_path = os.path.join(temp_module_dir, "scripts", "db_query.py")

        # Step 1: Analyze
        analysis = analyze_module(db_query_path)
        assert "error" not in analysis
        assert len(analysis["actions"]) == 1
        assert "temp-status" in analysis["actions"]

        # Step 2: Generate
        spec = {
            "action_name": "temp-add-item",
            "parameters": [
                {"name": "name", "type": "str", "required": True, "description": "Item name"},
                {"name": "company-id", "type": "str", "required": True, "description": "Company"},
            ],
            "description": "Add a new item to tempclaw",
            "table_name": "temp_item",
        }
        code = generate_feature_code(spec, analysis)
        assert "def temp_add_item(args):" in code
        assert "Table(" in code  # Module uses pypika

        # Verify generated code is valid Python
        try:
            ast.parse(code)
        except SyntaxError as e:
            pytest.fail(f"Generated code has syntax error: {e}\n\n{code}")

        # Step 3: Insert
        result = insert_feature(db_query_path, code, "temp-add-item")
        assert result["success"] is True, f"Insert failed: {result.get('error')}"
        assert result["action_added"] == "temp-add-item"
        assert result["function_added"] == "temp_add_item"
        assert result["lines_added"] > 0
        assert os.path.isfile(result["backup_path"])

        # Step 4: Validate
        validation = validate_insertion(db_query_path, "temp-add-item")
        syntax_check = next(c for c in validation["checks"] if c["name"] == "syntax_valid")
        assert syntax_check["passed"] is True

        action_check = next(c for c in validation["checks"] if c["name"] == "action_in_dict")
        assert action_check["passed"] is True

        func_check = next(c for c in validation["checks"] if c["name"] == "function_exists")
        assert func_check["passed"] is True

        # Step 5: Verify final file structure
        with open(db_query_path, "r") as f:
            final_source = f.read()

        # ACTIONS dict now has 2 entries
        assert '"temp-status"' in final_source
        assert '"temp-add-item"' in final_source
        assert "def temp_add_item(args):" in final_source
        assert "def handle_status(args):" in final_source

        # Verify the whole file compiles
        compile(final_source, db_query_path, "exec")

    def test_full_cycle_multiple_insertions(self, temp_module_dir):
        """Insert multiple features sequentially — all should coexist."""
        db_query_path = os.path.join(temp_module_dir, "scripts", "db_query.py")

        analysis = analyze_module(db_query_path)

        # Insert feature 1
        spec1 = {
            "action_name": "temp-add-widget",
            "parameters": [
                {"name": "name", "type": "str", "required": True, "description": "Name"},
            ],
            "description": "Add a widget",
        }
        code1 = generate_feature_code(spec1, analysis)
        r1 = insert_feature(db_query_path, code1, "temp-add-widget")
        assert r1["success"] is True

        # Re-analyze after first insertion
        analysis2 = analyze_module(db_query_path)
        assert len(analysis2["actions"]) == 2

        # Insert feature 2
        spec2 = {
            "action_name": "temp-list-widgets",
            "parameters": [],
            "description": "List widgets",
            "table_name": "temp_widget",
        }
        code2 = generate_feature_code(spec2, analysis2)
        r2 = insert_feature(db_query_path, code2, "temp-list-widgets")
        assert r2["success"] is True

        # Final state
        analysis3 = analyze_module(db_query_path)
        assert len(analysis3["actions"]) == 3
        assert "temp-status" in analysis3["actions"]
        assert "temp-add-widget" in analysis3["actions"]
        assert "temp-list-widgets" in analysis3["actions"]

        # Whole file compiles
        with open(db_query_path, "r") as f:
            final_source = f.read()
        compile(final_source, db_query_path, "exec")

    def test_full_cycle_financial_feature(self, temp_module_dir):
        """Insert a financial feature with Decimal handling."""
        db_query_path = os.path.join(temp_module_dir, "scripts", "db_query.py")

        analysis = analyze_module(db_query_path)
        spec = {
            "action_name": "temp-add-payment",
            "parameters": [
                {"name": "amount", "type": "decimal", "required": True,
                 "description": "Payment amount", "is_financial": True},
                {"name": "company-id", "type": "str", "required": True,
                 "description": "Company ID"},
            ],
            "description": "Record a payment",
            "table_name": "temp_payment",
            "is_financial": True,
        }
        code = generate_feature_code(spec, analysis)
        assert "Decimal" in code

        result = insert_feature(db_query_path, code, "temp-add-payment")
        assert result["success"] is True

        with open(db_query_path, "r") as f:
            final_source = f.read()
        compile(final_source, db_query_path, "exec")


# ===========================================================================
# Test: helper functions
# ===========================================================================

class TestHelpers:
    """Test internal helper functions."""

    def test_action_to_func_name(self):
        assert _action_to_func_name("add-customer") == "add_customer"
        assert _action_to_func_name("sell-close-order") == "sell_close_order"
        assert _action_to_func_name("status") == "status"

    def test_extract_verb(self):
        assert _extract_verb("add-customer") == "add"
        assert _extract_verb("groom-add-pet") == "add"
        assert _extract_verb("sell-list-orders") == "list"
        assert _extract_verb("create-invoice") == "create"
        assert _extract_verb("submit-sales-order") == "submit"

    def test_validate_syntax_good(self):
        ok, err = _validate_syntax("x = 1\ny = 2\n")
        assert ok is True
        assert err is None

    def test_validate_syntax_bad(self):
        ok, err = _validate_syntax("def foo(:\n    pass\n")
        assert ok is False
        assert err is not None

    def test_find_actions_dict_simple(self):
        lines = [
            "ACTIONS = {\n",
            '    "add-thing": add_thing,\n',
            '    "list-things": list_things,\n',
            "}\n",
        ]
        start, end, actions = _find_actions_dict(lines)
        assert start == 0
        assert end == 3
        assert actions == {"add-thing": "add_thing", "list-things": "list_things"}

    def test_find_actions_dict_multiline(self):
        lines = [
            "# some comment\n",
            "x = 42\n",
            "ACTIONS = {\n",
            '    "do-x": do_x,\n',
            '    "do-y": do_y,\n',
            '    "do-z": do_z,\n',
            "}\n",
            "\n",
            "def main():\n",
        ]
        start, end, actions = _find_actions_dict(lines)
        assert start == 2
        assert end == 6
        assert len(actions) == 3

    def test_analyze_nonexistent_file(self):
        result = analyze_module("/nonexistent/path/db_query.py")
        assert "error" in result

    def test_analyze_directory_resolution(self, temp_module_dir):
        """analyze_module should accept a module directory and find scripts/db_query.py."""
        result = analyze_module(temp_module_dir)
        assert "error" not in result
        assert result["file_path"].endswith("db_query.py")
