"""Tests for ERPClaw OS Auto-Test Generator (P1-9).

Covers:
- generate_feature_test: produces valid Python, has happy + error paths
- insert_feature_test: appends to existing file, creates new file, validates syntax
"""
import ast
import os
import sys

import pytest

# Make the erpclaw-os package importable
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(TESTS_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from in_module_generator import (
    generate_feature_test,
    insert_feature_test,
)


# ---------------------------------------------------------------------------
# Sample feature specs
# ---------------------------------------------------------------------------

SIMPLE_FEATURE_SPEC = {
    "action_name": "sell-close-order",
    "parameters": [
        {"name": "order-id", "type": "str", "required": True, "description": "The order UUID"},
        {"name": "company-id", "type": "str", "required": True, "description": "Company UUID"},
        {"name": "close-reason", "type": "str", "required": False, "description": "Reason for closing"},
    ],
    "description": "Close a sales order to prevent further deliveries.",
    "table_name": "sales_order",
}

FINANCIAL_FEATURE_SPEC = {
    "action_name": "add-payment-entry",
    "parameters": [
        {"name": "company-id", "type": "str", "required": True, "description": "Company UUID"},
        {"name": "amount", "type": "decimal", "required": True, "description": "Payment amount", "is_financial": True},
        {"name": "payment-date", "type": "str", "required": False, "description": "Date of payment"},
    ],
    "description": "Record a payment entry.",
    "table_name": "payment_entry",
    "is_financial": True,
}

LIST_FEATURE_SPEC = {
    "action_name": "list-overdue-invoices",
    "parameters": [
        {"name": "company-id", "type": "str", "required": True, "description": "Company UUID"},
        {"name": "days-overdue", "type": "int", "required": False, "description": "Minimum days overdue"},
    ],
    "description": "List all overdue invoices.",
    "table_name": "sales_invoice",
}

NO_REQUIRED_PARAMS_SPEC = {
    "action_name": "get-system-status",
    "parameters": [
        {"name": "verbose", "type": "bool", "required": False, "description": "Show detailed status"},
    ],
    "description": "Get system health status.",
}

SIMPLE_MODULE_ANALYSIS = {
    "indent_style": "    ",
    "uses_ok_err": True,
    "uses_pypika": True,
    "uses_decimal": True,
}


# ---------------------------------------------------------------------------
# Test: generate_feature_test produces valid Python
# ---------------------------------------------------------------------------

class TestGenerateFeatureTest:
    def test_generates_valid_python(self):
        """Generated test code is syntactically valid Python."""
        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        # Should not raise SyntaxError
        ast.parse(code)

    def test_has_happy_path_test(self):
        """Generated code contains a happy path test function."""
        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        assert "def test_sell_close_order_happy_path" in code

    def test_has_error_path_test(self):
        """Generated code contains an error path test function for missing required param."""
        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        assert "def test_sell_close_order_missing_order_id" in code

    def test_happy_and_error_paths_exist(self):
        """Both happy path and error path tests are present."""
        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        assert "happy_path" in code
        assert "missing_" in code

    def test_has_test_class(self):
        """Generated code wraps tests in a class."""
        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        assert "class TestGenerated" in code

    def test_has_assertions(self):
        """Generated tests contain assert statements."""
        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        assert "assert is_ok" in code
        assert "assert is_error" in code

    def test_financial_feature_uses_decimal_values(self):
        """Financial features use decimal string values in test params."""
        code = generate_feature_test(FINANCIAL_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        ast.parse(code)
        assert '"100.00"' in code

    def test_list_action_asserts_rows_or_count(self):
        """List actions assert rows or total_count in response."""
        code = generate_feature_test(LIST_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        ast.parse(code)
        assert "rows" in code or "total_count" in code

    def test_no_required_params_generates_minimal_error_test(self):
        """When no required params, generates a minimal empty call test."""
        code = generate_feature_test(NO_REQUIRED_PARAMS_SPEC, SIMPLE_MODULE_ANALYSIS)
        ast.parse(code)
        assert "def test_get_system_status_" in code

    def test_produces_valid_python_for_all_specs(self):
        """All sample feature specs produce valid Python."""
        for spec in [SIMPLE_FEATURE_SPEC, FINANCIAL_FEATURE_SPEC, LIST_FEATURE_SPEC, NO_REQUIRED_PARAMS_SPEC]:
            code = generate_feature_test(spec, SIMPLE_MODULE_ANALYSIS)
            try:
                ast.parse(code)
            except SyntaxError as e:
                pytest.fail(f"Syntax error for {spec['action_name']}: {e}")

    def test_add_action_asserts_id(self):
        """Add actions assert 'id' in response."""
        spec = {
            "action_name": "add-widget",
            "parameters": [
                {"name": "name", "type": "str", "required": True, "description": "Widget name"},
                {"name": "company-id", "type": "str", "required": True, "description": "Company UUID"},
            ],
            "description": "Add a new widget.",
            "table_name": "widget",
        }
        code = generate_feature_test(spec, SIMPLE_MODULE_ANALYSIS)
        assert '"id" in result' in code

    def test_uses_env_for_company_id(self):
        """Generated tests use env['company_id'] for company-id params."""
        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        assert 'env["company_id"]' in code

    def test_date_params_get_iso_value(self):
        """Date params get a realistic ISO date value."""
        code = generate_feature_test(FINANCIAL_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        assert "2026-01-15" in code


# ---------------------------------------------------------------------------
# Test: insert_feature_test appends to existing file
# ---------------------------------------------------------------------------

class TestInsertFeatureTest:
    def test_insert_appends_to_existing(self, tmp_path):
        """Test code is appended to an existing test file."""
        test_file = tmp_path / "test_existing.py"
        test_file.write_text(
            '"""Existing tests."""\n'
            'import pytest\n'
            '\n'
            'class TestExisting:\n'
            '    def test_something(self):\n'
            '        assert True\n'
        )

        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        result = insert_feature_test(str(test_file), code)

        assert result["success"] is True
        assert result["lines_added"] > 0
        assert result["created_new"] is False
        assert result["backup_path"] is not None

        # Verify the combined file is valid Python
        content = test_file.read_text()
        ast.parse(content)
        assert "TestExisting" in content
        assert "TestGenerated" in content

    def test_insert_creates_new_file(self, tmp_path):
        """Test code creates a new file if it does not exist."""
        test_file = tmp_path / "test_new.py"

        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        result = insert_feature_test(str(test_file), code)

        assert result["success"] is True
        assert result["created_new"] is True
        assert test_file.exists()

        # Verify the file is valid Python
        content = test_file.read_text()
        ast.parse(content)
        assert "import pytest" in content

    def test_insert_creates_backup(self, tmp_path):
        """Existing file gets a .bak backup before modification."""
        test_file = tmp_path / "test_backup.py"
        test_file.write_text(
            '"""Original content."""\n'
            'import pytest\n'
        )

        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        result = insert_feature_test(str(test_file), code)

        assert result["success"] is True
        backup_path = result["backup_path"]
        assert os.path.isfile(backup_path)
        assert backup_path.endswith(".bak")

    def test_insert_rejects_invalid_syntax(self, tmp_path):
        """Invalid test code is rejected."""
        test_file = tmp_path / "test_invalid.py"
        test_file.write_text(
            '"""Existing."""\n'
            'import pytest\n'
        )

        bad_code = "def test_broken(:\n    pass"  # Syntax error: missing closing paren
        result = insert_feature_test(str(test_file), bad_code)

        assert result["success"] is False
        assert "syntax error" in result["error"].lower()

    def test_insert_preserves_original_on_failure(self, tmp_path):
        """On failure, the original file is preserved."""
        test_file = tmp_path / "test_preserve.py"
        original = '"""Original."""\nimport pytest\n'
        test_file.write_text(original)

        bad_code = "def broken(:\n    pass"
        insert_feature_test(str(test_file), bad_code)

        # Original should be intact
        assert test_file.read_text() == original

    def test_insert_to_nested_dir(self, tmp_path):
        """Creates parent directories if they do not exist."""
        test_file = tmp_path / "sub" / "dir" / "test_nested.py"
        code = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        result = insert_feature_test(str(test_file), code)

        assert result["success"] is True
        assert result["created_new"] is True
        assert test_file.exists()

    def test_insert_multiple_features(self, tmp_path):
        """Multiple features can be appended sequentially."""
        test_file = tmp_path / "test_multi.py"
        test_file.write_text('"""Tests."""\nimport pytest\n')

        code1 = generate_feature_test(SIMPLE_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        result1 = insert_feature_test(str(test_file), code1)
        assert result1["success"] is True

        code2 = generate_feature_test(FINANCIAL_FEATURE_SPEC, SIMPLE_MODULE_ANALYSIS)
        result2 = insert_feature_test(str(test_file), code2)
        assert result2["success"] is True

        content = test_file.read_text()
        ast.parse(content)
        assert "SellCloseOrder" in content
        assert "AddPaymentEntry" in content
