#!/usr/bin/env python3
"""Tests for ERPClaw OS Feature Completeness Matrix (Phase 4, P1-7).

Tests the feature_matrix.py module which provides a machine-readable matrix
of expected ERP features per domain, compared against what ERPClaw has.
"""
import os
import sys
import tempfile
import textwrap

import pytest

# Add erpclaw-os directory to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(SCRIPT_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from feature_matrix import (
    EXPECTED_FEATURES,
    DOMAIN_SCRIPT_PATHS,
    extract_actions_from_file,
    get_domain_actions,
    check_feature_completeness,
    get_domain_score,
    get_all_domain_scores,
    handle_check_feature_completeness,
    handle_list_feature_matrix,
)

# Path to real src/ directory for integration-style tests
SRC_ROOT = os.path.abspath(os.path.join(OS_DIR, "..", "..", ".."))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_src(tmp_path):
    """Create a temporary src directory with minimal db_query.py files."""
    # Create a selling domain with some actions
    selling_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-selling"
    selling_dir.mkdir(parents=True)
    (selling_dir / "db_query.py").write_text(textwrap.dedent("""\
        def add_quotation(): pass
        def update_quotation(): pass
        def list_quotations(): pass
        def get_quotation(): pass
        def add_sales_order(): pass
        def submit_sales_order(): pass
        def cancel_sales_order(): pass
        def create_delivery_note(): pass
        def submit_delivery_note(): pass
        def create_sales_invoice(): pass
        def submit_sales_invoice(): pass
        def create_credit_note(): pass
        def add_recurring_template(): pass
        def generate_recurring_invoices(): pass
        def status_action(): pass

        ACTIONS = {
            "add-quotation": add_quotation,
            "update-quotation": update_quotation,
            "list-quotations": list_quotations,
            "get-quotation": get_quotation,
            "add-sales-order": add_sales_order,
            "submit-sales-order": submit_sales_order,
            "cancel-sales-order": cancel_sales_order,
            "create-delivery-note": create_delivery_note,
            "submit-delivery-note": submit_delivery_note,
            "create-sales-invoice": create_sales_invoice,
            "submit-sales-invoice": submit_sales_invoice,
            "create-credit-note": create_credit_note,
            "add-recurring-template": add_recurring_template,
            "generate-recurring-invoices": generate_recurring_invoices,
            "status": status_action,
        }
    """))

    # Create a buying domain with some actions
    buying_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-buying"
    buying_dir.mkdir(parents=True)
    (buying_dir / "db_query.py").write_text(textwrap.dedent("""\
        def add_supplier(): pass
        def update_supplier(): pass
        def list_suppliers(): pass
        def add_purchase_order(): pass
        def submit_purchase_order(): pass
        def cancel_purchase_order(): pass
        def create_purchase_receipt(): pass
        def submit_purchase_receipt(): pass
        def create_purchase_invoice(): pass
        def submit_purchase_invoice(): pass
        def add_rfq(): pass
        def submit_rfq(): pass
        def add_supplier_quotation(): pass
        def compare_supplier_quotations(): pass
        def create_debit_note(): pass
        def add_landed_cost_voucher(): pass

        ACTIONS = {
            "add-supplier": add_supplier,
            "update-supplier": update_supplier,
            "list-suppliers": list_suppliers,
            "add-purchase-order": add_purchase_order,
            "submit-purchase-order": submit_purchase_order,
            "cancel-purchase-order": cancel_purchase_order,
            "create-purchase-receipt": create_purchase_receipt,
            "submit-purchase-receipt": submit_purchase_receipt,
            "create-purchase-invoice": create_purchase_invoice,
            "submit-purchase-invoice": submit_purchase_invoice,
            "add-rfq": add_rfq,
            "submit-rfq": submit_rfq,
            "add-supplier-quotation": add_supplier_quotation,
            "compare-supplier-quotations": compare_supplier_quotations,
            "create-debit-note": create_debit_note,
            "add-landed-cost-voucher": add_landed_cost_voucher,
        }
    """))

    # Create inventory domain with some actions
    inventory_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-inventory"
    inventory_dir.mkdir(parents=True)
    (inventory_dir / "db_query.py").write_text(textwrap.dedent("""\
        def add_item(): pass
        def update_item(): pass
        def get_item(): pass
        def list_items(): pass
        def add_warehouse(): pass
        def list_warehouses(): pass
        def add_stock_entry(): pass
        def submit_stock_entry(): pass
        def get_stock_balance(): pass
        def stock_balance_report(): pass
        def add_batch(): pass
        def list_batches(): pass
        def add_serial_number(): pass
        def list_serial_numbers(): pass
        def add_price_list(): pass
        def add_item_price(): pass
        def get_item_price(): pass
        def add_stock_reconciliation(): pass
        def submit_stock_reconciliation(): pass
        def check_reorder(): pass

        ACTIONS = {
            "add-item": add_item,
            "update-item": update_item,
            "get-item": get_item,
            "list-items": list_items,
            "add-warehouse": add_warehouse,
            "list-warehouses": list_warehouses,
            "add-stock-entry": add_stock_entry,
            "submit-stock-entry": submit_stock_entry,
            "get-stock-balance": get_stock_balance,
            "stock-balance-report": stock_balance_report,
            "add-batch": add_batch,
            "list-batches": list_batches,
            "add-serial-number": add_serial_number,
            "list-serial-numbers": list_serial_numbers,
            "add-price-list": add_price_list,
            "add-item-price": add_item_price,
            "get-item-price": get_item_price,
            "add-stock-reconciliation": add_stock_reconciliation,
            "submit-stock-reconciliation": submit_stock_reconciliation,
            "check-reorder": check_reorder,
        }
    """))

    # Create payroll domain with some actions
    payroll_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-payroll"
    payroll_dir.mkdir(parents=True)
    (payroll_dir / "db_query.py").write_text(textwrap.dedent("""\
        def add_salary_component(): pass
        def add_salary_structure(): pass
        def add_salary_assignment(): pass
        def create_payroll_run(): pass
        def generate_salary_slips(): pass
        def submit_payroll_run(): pass
        def cancel_payroll_run(): pass
        def add_income_tax_slab(): pass
        def update_fica_config(): pass
        def add_garnishment(): pass
        def list_garnishments(): pass
        def generate_w2_data(): pass

        ACTIONS = {
            "add-salary-component": add_salary_component,
            "add-salary-structure": add_salary_structure,
            "add-salary-assignment": add_salary_assignment,
            "create-payroll-run": create_payroll_run,
            "generate-salary-slips": generate_salary_slips,
            "submit-payroll-run": submit_payroll_run,
            "cancel-payroll-run": cancel_payroll_run,
            "add-income-tax-slab": add_income_tax_slab,
            "update-fica-config": update_fica_config,
            "add-garnishment": add_garnishment,
            "list-garnishments": list_garnishments,
            "generate-w2-data": generate_w2_data,
        }
    """))

    # Create HR domain
    hr_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-hr"
    hr_dir.mkdir(parents=True)
    (hr_dir / "db_query.py").write_text(textwrap.dedent("""\
        def add_employee(): pass
        def update_employee(): pass
        def get_employee(): pass
        def list_employees(): pass
        def add_department(): pass
        def list_departments(): pass
        def add_leave_type(): pass
        def add_leave_allocation(): pass
        def add_leave_application(): pass
        def approve_leave(): pass
        def get_leave_balance(): pass
        def mark_attendance(): pass
        def list_attendance(): pass
        def add_expense_claim(): pass
        def submit_expense_claim(): pass
        def approve_expense_claim(): pass
        def record_lifecycle_event(): pass
        def add_holiday_list(): pass

        ACTIONS = {
            "add-employee": add_employee,
            "update-employee": update_employee,
            "get-employee": get_employee,
            "list-employees": list_employees,
            "add-department": add_department,
            "list-departments": list_departments,
            "add-leave-type": add_leave_type,
            "add-leave-allocation": add_leave_allocation,
            "add-leave-application": add_leave_application,
            "approve-leave": approve_leave,
            "get-leave-balance": get_leave_balance,
            "mark-attendance": mark_attendance,
            "list-attendance": list_attendance,
            "add-expense-claim": add_expense_claim,
            "submit-expense-claim": submit_expense_claim,
            "approve-expense-claim": approve_expense_claim,
            "record-lifecycle-event": record_lifecycle_event,
            "add-holiday-list": add_holiday_list,
        }
    """))

    # Create manufacturing domain
    mfg_dir = tmp_path / "erpclaw-addons" / "erpclaw-ops" / "scripts" / "erpclaw-manufacturing"
    mfg_dir.mkdir(parents=True)
    (mfg_dir / "db_query.py").write_text(textwrap.dedent("""\
        def add_bom(): pass
        def update_bom(): pass
        def get_bom(): pass
        def list_boms(): pass
        def explode_bom(): pass
        def add_operation(): pass
        def add_workstation(): pass
        def add_routing(): pass
        def add_work_order(): pass
        def start_work_order(): pass
        def complete_work_order(): pass
        def cancel_work_order(): pass
        def create_job_card(): pass
        def complete_job_card(): pass
        def create_production_plan(): pass
        def run_mrp(): pass
        def generate_work_orders(): pass
        def transfer_materials(): pass
        def add_subcontracting_order(): pass

        ACTIONS = {
            "add-bom": add_bom,
            "update-bom": update_bom,
            "get-bom": get_bom,
            "list-boms": list_boms,
            "explode-bom": explode_bom,
            "add-operation": add_operation,
            "add-workstation": add_workstation,
            "add-routing": add_routing,
            "add-work-order": add_work_order,
            "start-work-order": start_work_order,
            "complete-work-order": complete_work_order,
            "cancel-work-order": cancel_work_order,
            "create-job-card": create_job_card,
            "complete-job-card": complete_job_card,
            "create-production-plan": create_production_plan,
            "run-mrp": run_mrp,
            "generate-work-orders": generate_work_orders,
            "transfer-materials": transfer_materials,
            "add-subcontracting-order": add_subcontracting_order,
        }
    """))

    return str(tmp_path)


@pytest.fixture
def mock_args(tmp_src):
    """Create a mock args object."""
    class Args:
        src_root = tmp_src
        domain = None
        db_path = None
    return Args()


# ---------------------------------------------------------------------------
# Test: EXPECTED_FEATURES structure
# ---------------------------------------------------------------------------

class TestExpectedFeaturesStructure:
    """Validate the EXPECTED_FEATURES data structure."""

    def test_all_domains_covered(self):
        """Every domain in EXPECTED_FEATURES is one of the 6 required domains."""
        required_domains = {"selling", "buying", "inventory", "manufacturing", "hr", "payroll"}
        actual_domains = set(EXPECTED_FEATURES.keys())
        assert required_domains == actual_domains, (
            f"Missing domains: {required_domains - actual_domains}, "
            f"Extra domains: {actual_domains - required_domains}"
        )

    def test_every_feature_has_required_fields(self):
        """Every feature entry has name, actions, priority, severity."""
        required_fields = {"name", "actions", "priority", "severity"}
        for domain, features in EXPECTED_FEATURES.items():
            for feat in features:
                missing = required_fields - set(feat.keys())
                assert not missing, (
                    f"Feature '{feat.get('name', '?')}' in {domain} "
                    f"missing fields: {missing}"
                )

    def test_priorities_are_valid(self):
        """All priorities are P1, P2, or P3."""
        valid_priorities = {"P1", "P2", "P3"}
        for domain, features in EXPECTED_FEATURES.items():
            for feat in features:
                assert feat["priority"] in valid_priorities, (
                    f"Feature '{feat['name']}' in {domain} has invalid priority: {feat['priority']}"
                )

    def test_severities_are_valid(self):
        """All severities are must-have or nice-to-have."""
        valid_severities = {"must-have", "nice-to-have"}
        for domain, features in EXPECTED_FEATURES.items():
            for feat in features:
                assert feat["severity"] in valid_severities, (
                    f"Feature '{feat['name']}' in {domain} has invalid severity: {feat['severity']}"
                )

    def test_actions_are_non_empty_lists(self):
        """Every feature has at least one expected action."""
        for domain, features in EXPECTED_FEATURES.items():
            for feat in features:
                assert isinstance(feat["actions"], list) and len(feat["actions"]) > 0, (
                    f"Feature '{feat['name']}' in {domain} has empty actions list"
                )

    def test_no_duplicate_feature_names_within_domain(self):
        """No duplicate feature names within the same domain."""
        for domain, features in EXPECTED_FEATURES.items():
            names = [f["name"] for f in features]
            assert len(names) == len(set(names)), (
                f"Duplicate feature names in {domain}: "
                f"{[n for n in names if names.count(n) > 1]}"
            )

    def test_minimum_features_per_domain(self):
        """Each domain has at least 5 expected features."""
        for domain, features in EXPECTED_FEATURES.items():
            assert len(features) >= 5, (
                f"Domain '{domain}' has only {len(features)} features, expected at least 5"
            )


# ---------------------------------------------------------------------------
# Test: Action extraction
# ---------------------------------------------------------------------------

class TestActionExtraction:
    """Test extracting actions from db_query.py files."""

    def test_extract_actions_from_file(self, tmp_src):
        """Extract actions from a test db_query.py file."""
        path = os.path.join(tmp_src, "erpclaw/scripts/erpclaw-selling/db_query.py")
        actions = extract_actions_from_file(path)
        assert "add-quotation" in actions
        assert "submit-sales-order" in actions
        assert "create-credit-note" in actions
        assert "status" in actions

    def test_extract_actions_nonexistent_file(self):
        """Return empty set for nonexistent file."""
        actions = extract_actions_from_file("/nonexistent/path/db_query.py")
        assert actions == set()

    def test_extract_actions_no_actions_dict(self, tmp_path):
        """Return empty set when file has no ACTIONS dict."""
        f = tmp_path / "no_actions.py"
        f.write_text("x = 1\ny = 2\n")
        assert extract_actions_from_file(str(f)) == set()

    def test_get_domain_actions(self, tmp_src):
        """Get actions for a specific domain."""
        actions = get_domain_actions(tmp_src, "selling")
        assert "add-quotation" in actions
        assert len(actions) > 10

    def test_get_domain_actions_unknown_domain(self, tmp_src):
        """Unknown domain returns empty set."""
        actions = get_domain_actions(tmp_src, "nonexistent-domain")
        assert actions == set()


# ---------------------------------------------------------------------------
# Test: Feature completeness — specific known features
# ---------------------------------------------------------------------------

class TestFeatureCompleteness:
    """Test feature completeness checking against known state."""

    def test_selling_has_quotation_crud(self, tmp_src):
        """Quotation CRUD should be detected as present in selling."""
        score = get_domain_score(tmp_src, "selling")
        present_names = [f["name"] for f in score["present_features"]]
        assert "quotation_crud" in present_names, (
            "quotation_crud should be present — selling has add-quotation, "
            "update-quotation, list-quotations, get-quotation"
        )

    def test_selling_missing_blanket_so(self, tmp_src):
        """Blanket SO should be detected as missing from selling."""
        missing = check_feature_completeness(tmp_src, domain="selling")
        missing_names = [f["feature"] for f in missing]
        assert "blanket_so" in missing_names, (
            "blanket_so should be missing — selling has no add-blanket-order action"
        )

    def test_selling_missing_so_amendment(self, tmp_src):
        """SO amendment should be detected as missing from selling."""
        missing = check_feature_completeness(tmp_src, domain="selling")
        missing_names = [f["feature"] for f in missing]
        assert "so_amendment" in missing_names

    def test_selling_missing_drop_shipment(self, tmp_src):
        """Drop shipment should be detected as missing from selling."""
        missing = check_feature_completeness(tmp_src, domain="selling")
        missing_names = [f["feature"] for f in missing]
        assert "drop_shipment" in missing_names

    def test_inventory_missing_fifo(self, tmp_src):
        """FIFO valuation should be detected as missing from inventory."""
        missing = check_feature_completeness(tmp_src, domain="inventory")
        missing_names = [f["feature"] for f in missing]
        assert "fifo_valuation" in missing_names, (
            "fifo_valuation should be missing — inventory has no FIFO actions"
        )

    def test_inventory_missing_projected_qty(self, tmp_src):
        """Projected qty should be detected as missing from inventory."""
        missing = check_feature_completeness(tmp_src, domain="inventory")
        missing_names = [f["feature"] for f in missing]
        assert "projected_qty" in missing_names

    def test_inventory_missing_item_variants(self, tmp_src):
        """Item variants should be detected as missing from inventory."""
        missing = check_feature_completeness(tmp_src, domain="inventory")
        missing_names = [f["feature"] for f in missing]
        assert "item_variants" in missing_names

    def test_payroll_missing_overtime(self, tmp_src):
        """Overtime calculation should be detected as missing from payroll."""
        missing = check_feature_completeness(tmp_src, domain="payroll")
        missing_names = [f["feature"] for f in missing]
        assert "overtime_calculation" in missing_names, (
            "overtime_calculation should be missing — payroll has no overtime actions"
        )

    def test_payroll_missing_multi_state(self, tmp_src):
        """Multi-state payroll should be detected as missing from payroll."""
        missing = check_feature_completeness(tmp_src, domain="payroll")
        missing_names = [f["feature"] for f in missing]
        assert "multi_state_payroll" in missing_names

    def test_payroll_missing_nacha(self, tmp_src):
        """NACHA/ACH should be detected as missing from payroll."""
        missing = check_feature_completeness(tmp_src, domain="payroll")
        missing_names = [f["feature"] for f in missing]
        assert "nacha_ach" in missing_names

    def test_buying_missing_three_way_match(self, tmp_src):
        """Three-way match should be detected as missing from buying."""
        missing = check_feature_completeness(tmp_src, domain="buying")
        missing_names = [f["feature"] for f in missing]
        assert "three_way_match" in missing_names

    def test_buying_missing_close_po(self, tmp_src):
        """Close PO should be detected as missing from buying."""
        missing = check_feature_completeness(tmp_src, domain="buying")
        missing_names = [f["feature"] for f in missing]
        assert "close_po" in missing_names

    def test_hr_missing_shift_management(self, tmp_src):
        """Shift management should be detected as missing from HR."""
        missing = check_feature_completeness(tmp_src, domain="hr")
        missing_names = [f["feature"] for f in missing]
        assert "shift_management" in missing_names

    def test_manufacturing_missing_co_products(self, tmp_src):
        """Co-products should be detected as missing from manufacturing."""
        missing = check_feature_completeness(tmp_src, domain="manufacturing")
        missing_names = [f["feature"] for f in missing]
        assert "co_products" in missing_names

    def test_manufacturing_missing_material_substitution(self, tmp_src):
        """Material substitution should be detected as missing from manufacturing."""
        missing = check_feature_completeness(tmp_src, domain="manufacturing")
        missing_names = [f["feature"] for f in missing]
        assert "material_substitution" in missing_names

    def test_present_features_not_in_missing(self, tmp_src):
        """Features that are present should NOT appear in the missing list."""
        missing = check_feature_completeness(tmp_src, domain="selling")
        missing_names = [f["feature"] for f in missing]
        # These should be present, not missing
        assert "quotation_crud" not in missing_names
        assert "sales_order_lifecycle" not in missing_names
        assert "delivery_note" not in missing_names
        assert "sales_invoice" not in missing_names
        assert "credit_note" not in missing_names
        assert "recurring_sales" not in missing_names


# ---------------------------------------------------------------------------
# Test: Domain score calculation
# ---------------------------------------------------------------------------

class TestDomainScore:
    """Test domain score calculation."""

    def test_domain_score_calculation(self, tmp_src):
        """Domain score correctly counts present vs missing features."""
        score = get_domain_score(tmp_src, "selling")
        assert score["domain"] == "selling"
        assert score["total_expected"] == len(EXPECTED_FEATURES["selling"])
        assert score["total_present"] + score["total_missing"] == score["total_expected"]
        assert 0.0 <= score["score_pct"] <= 100.0

        # Selling should have most P1 features present
        assert score["total_present"] > 0
        assert score["total_missing"] > 0  # Some features are definitely missing

    def test_domain_score_present_plus_missing_equals_total(self, tmp_src):
        """present + missing always equals total_expected for every domain."""
        for domain in EXPECTED_FEATURES:
            score = get_domain_score(tmp_src, domain)
            assert score["total_present"] + score["total_missing"] == score["total_expected"], (
                f"Domain '{domain}': present({score['total_present']}) + "
                f"missing({score['total_missing']}) != total({score['total_expected']})"
            )

    def test_domain_score_unknown_domain(self, tmp_src):
        """Unknown domain returns zero scores."""
        score = get_domain_score(tmp_src, "nonexistent")
        assert score["total_expected"] == 0
        assert score["total_present"] == 0
        assert score["score_pct"] == 0.0

    def test_all_domain_scores(self, tmp_src):
        """All domain scores aggregate correctly."""
        all_scores = get_all_domain_scores(tmp_src)
        assert "overall_score_pct" in all_scores
        assert all_scores["total_expected"] > 0
        assert all_scores["total_present"] + all_scores["total_missing"] == all_scores["total_expected"]
        assert len(all_scores["domains"]) == len(EXPECTED_FEATURES)

    def test_score_pct_is_percentage(self, tmp_src):
        """Score percentages are between 0 and 100."""
        for domain in EXPECTED_FEATURES:
            score = get_domain_score(tmp_src, domain)
            assert 0.0 <= score["score_pct"] <= 100.0, (
                f"Domain '{domain}' has invalid score: {score['score_pct']}"
            )


# ---------------------------------------------------------------------------
# Test: Missing features sorted by priority
# ---------------------------------------------------------------------------

class TestMissingSorting:
    """Test that missing features are correctly sorted by priority."""

    def test_p1_before_p2(self, tmp_src):
        """P1 features should appear before P2 in the missing list."""
        missing = check_feature_completeness(tmp_src)
        priorities = [f["priority"] for f in missing]
        # Find transitions from P2 back to P1 — should never happen
        for i in range(1, len(priorities)):
            if priorities[i] == "P1" and priorities[i - 1] in ("P2", "P3"):
                pytest.fail(
                    f"P1 feature at index {i} appears after {priorities[i-1]} "
                    f"at index {i-1} — sorting broken"
                )

    def test_must_have_before_nice_to_have_within_priority(self, tmp_src):
        """Within same priority, must-have features appear before nice-to-have."""
        missing = check_feature_completeness(tmp_src)
        # Group by priority
        by_priority = {}
        for f in missing:
            by_priority.setdefault(f["priority"], []).append(f)

        for p, features in by_priority.items():
            severities = [f["severity"] for f in features]
            saw_nice = False
            for s in severities:
                if s == "nice-to-have":
                    saw_nice = True
                elif s == "must-have" and saw_nice:
                    pytest.fail(
                        f"In priority {p}: must-have appears after nice-to-have"
                    )


# ---------------------------------------------------------------------------
# Test: Action handlers
# ---------------------------------------------------------------------------

class TestActionHandlers:
    """Test the action handlers for wiring into db_query.py."""

    def test_handle_check_feature_completeness(self, mock_args):
        """check-feature-completeness returns structured result."""
        result = handle_check_feature_completeness(mock_args)
        assert result["result"] == "ok"
        assert "total_missing" in result
        assert "total_expected" in result
        assert "total_present" in result
        assert "overall_score_pct" in result
        assert "missing_by_priority" in result
        assert "domain_scores" in result
        assert "missing_features" in result
        assert result["total_missing"] > 0

    def test_handle_check_feature_completeness_with_domain(self, mock_args):
        """check-feature-completeness filters by domain when given."""
        mock_args.domain = "selling"
        result = handle_check_feature_completeness(mock_args)
        assert result["result"] == "ok"
        # All missing features should be from selling
        for feat in result["missing_features"]:
            assert feat["domain"] == "selling"
        # Only selling in domain_scores
        assert list(result["domain_scores"].keys()) == ["selling"]

    def test_handle_check_feature_completeness_invalid_domain(self, mock_args):
        """check-feature-completeness returns error for invalid domain."""
        mock_args.domain = "nonexistent"
        result = handle_check_feature_completeness(mock_args)
        assert "error" in result
        assert "available_domains" in result

    def test_handle_check_feature_completeness_no_src_root(self):
        """check-feature-completeness returns error without src-root."""
        class Args:
            src_root = None
            domain = None
        result = handle_check_feature_completeness(Args())
        assert "error" in result

    def test_handle_list_feature_matrix(self):
        """list-feature-matrix returns the full matrix."""
        class Args:
            domain = None
        result = handle_list_feature_matrix(Args())
        assert result["result"] == "ok"
        assert result["total_features"] > 0
        assert len(result["domains"]) == len(EXPECTED_FEATURES)
        assert "matrix" in result

    def test_handle_list_feature_matrix_single_domain(self):
        """list-feature-matrix returns single domain when filtered."""
        class Args:
            domain = "selling"
        result = handle_list_feature_matrix(Args())
        assert result["result"] == "ok"
        assert result["domains"] == ["selling"]
        assert "selling" in result["matrix"]
        assert len(result["matrix"]) == 1


# ---------------------------------------------------------------------------
# Test: Integration with real src/ directory
# ---------------------------------------------------------------------------

class TestRealSrcIntegration:
    """Integration tests against the actual ERPClaw src/ directory.

    These tests verify that the feature matrix correctly detects
    known present and missing features in the real codebase.
    """

    @pytest.mark.skipif(
        not os.path.isdir(os.path.join(SRC_ROOT, "erpclaw")),
        reason="Real src/ directory not available",
    )
    def test_real_selling_has_quotation_crud(self):
        """Real codebase: selling should have quotation CRUD."""
        score = get_domain_score(SRC_ROOT, "selling")
        present_names = [f["name"] for f in score["present_features"]]
        assert "quotation_crud" in present_names

    @pytest.mark.skipif(
        not os.path.isdir(os.path.join(SRC_ROOT, "erpclaw")),
        reason="Real src/ directory not available",
    )
    def test_real_selling_missing_blanket_so(self):
        """Real codebase: selling should be missing blanket SO."""
        missing = check_feature_completeness(SRC_ROOT, domain="selling")
        missing_names = [f["feature"] for f in missing]
        assert "blanket_so" in missing_names

    @pytest.mark.skipif(
        not os.path.isdir(os.path.join(SRC_ROOT, "erpclaw")),
        reason="Real src/ directory not available",
    )
    def test_real_inventory_missing_fifo(self):
        """Real codebase: inventory should be missing FIFO valuation."""
        missing = check_feature_completeness(SRC_ROOT, domain="inventory")
        missing_names = [f["feature"] for f in missing]
        assert "fifo_valuation" in missing_names

    @pytest.mark.skipif(
        not os.path.isdir(os.path.join(SRC_ROOT, "erpclaw")),
        reason="Real src/ directory not available",
    )
    def test_real_payroll_missing_overtime(self):
        """Real codebase: payroll should be missing overtime."""
        missing = check_feature_completeness(SRC_ROOT, domain="payroll")
        missing_names = [f["feature"] for f in missing]
        assert "overtime_calculation" in missing_names

    @pytest.mark.skipif(
        not os.path.isdir(os.path.join(SRC_ROOT, "erpclaw")),
        reason="Real src/ directory not available",
    )
    def test_real_all_domain_scores(self):
        """Real codebase: all domain scores should be computable."""
        all_scores = get_all_domain_scores(SRC_ROOT)
        assert all_scores["total_expected"] > 0
        assert all_scores["overall_score_pct"] > 0  # At least some features exist
        assert all_scores["total_missing"] > 0  # Known gaps exist
