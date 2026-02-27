"""Shared helpers for cross-skill integration tests.

Provides a unified _call_action() that can invoke action functions from
any of the 6 Phase 1 skills: setup, gl, journals, payments, tax, reports.
"""
import argparse
import importlib
import io
import json
import os
import sys
import threading
import uuid

# ---------------------------------------------------------------------------
# Skill module loading — import ACTIONS from each skill's db_query.py
# ---------------------------------------------------------------------------

SKILLS_DIR = os.path.dirname(os.path.dirname(__file__))

# We import each skill's db_query module under a unique name to avoid clashes.
# Each module has an ACTIONS dict mapping action names to functions.

def _load_skill_module(skill_name):
    """Import a skill's db_query.py and return the module."""
    scripts_dir = os.path.join(SKILLS_DIR, skill_name, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(
        f"{skill_name.replace('-','_')}_db_query",
        os.path.join(scripts_dir, "db_query.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Lazy-load modules on first access
_SKILL_MODULES = {}

def _get_skill_actions(skill_name):
    """Get the ACTIONS dict for a skill. Caches the module."""
    if skill_name not in _SKILL_MODULES:
        _SKILL_MODULES[skill_name] = _load_skill_module(skill_name)
    return _SKILL_MODULES[skill_name].ACTIONS


# ---------------------------------------------------------------------------
# Superset default args — covers ALL argparse flags across all 24 skills
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Common identifiers
    "company_id": None,
    "account_id": None,
    "posting_date": None,
    "from_date": None,
    "to_date": None,
    "as_of_date": None,
    "name": None,

    # GL skill
    "template": None,
    "root_type": None,
    "account_type": None,
    "account_number": None,
    "parent_id": None,
    "currency": None,
    "is_group": False,
    "is_frozen": None,
    "include_frozen": False,
    "search": None,
    "voucher_type": None,
    "voucher_id": None,
    "entries": None,
    "is_cancelled": None,
    "party_type": None,
    "party_id": None,
    "start_date": None,
    "end_date": None,
    "fiscal_year_id": None,
    "closing_account_id": None,
    "budget_amount": None,
    "action_if_exceeded": None,
    "cost_center_id": None,
    "entity_type": None,
    "limit": None,
    "offset": None,

    # Journals skill
    "journal_entry_id": None,
    "entry_type": None,
    "remark": None,
    "lines": None,
    "amended_from": None,
    "je_status": None,

    # Payments skill
    "payment_entry_id": None,
    "payment_type": None,
    "paid_from_account": None,
    "paid_to_account": None,
    "paid_amount": None,
    "payment_currency": "USD",
    "exchange_rate": "1",
    "reference_number": None,
    "reference_date": None,
    "allocations": None,
    "allocated_amount": None,
    "ple_amount": None,
    "against_voucher_type": None,
    "against_voucher_id": None,
    "bank_account_id": None,
    "pe_status": None,

    # Tax skill
    "tax_template_id": None,
    "tax_type": None,
    "is_default": False,
    "tax_category_id": None,
    "tax_lines": None,
    "description": None,
    "customer_id": None,
    "supplier_id": None,
    "customer_group": None,
    "supplier_group": None,
    "item_id": None,
    "item_group": None,
    "billing_state": None,
    "shipping_state": None,
    "priority": None,
    "net_total": None,
    "items_json": None,
    "tax_template_lines_json": None,
    "category_code": None,
    "single_threshold": None,
    "cumulative_threshold": None,
    "tax_on_excess_amount": False,
    "wh_rate": None,
    "effective_from": None,
    "effective_to": None,
    "wh_account_id": None,
    "wh_category_id": None,
    "fiscal_year": None,
    "taxable_amount": None,
    "withheld_amount": None,
    "taxable_voucher_type": None,
    "taxable_voucher_id": None,
    "withholding_voucher_type": None,
    "withholding_voucher_id": None,
    "is_1099_payment": False,
    "item_tax_rate": None,

    # Reports skill
    "aging_buckets": "30,60,90,120",
    "periodicity": "annual",
    "periods": None,
    "project_id": None,

    # Setup skill
    "abbr": None,
    "default_currency": None,
    "country": None,
    "fiscal_year_start_month": None,
    "chart_template": None,
    "data_json": None,
    "key": None,
    "value": None,

    # Selling skill
    "customer_type": None,
    "credit_limit": None,
    "exempt_from_sales_tax": None,
    "primary_address": None,
    "primary_contact": None,
    "quotation_id": None,
    "valid_till": None,
    "sales_order_id": None,
    "delivery_date": None,
    "delivery_note_id": None,
    "sales_invoice_id": None,
    "due_date": None,
    "against_invoice_id": None,
    "reason": None,
    "amount": None,
    "commission_rate": None,
    "template_id": None,
    "frequency": None,
    "template_status": None,
    "doc_status": None,

    # Buying skill
    "supplier_type": None,
    "tax_id": None,
    "is_1099_vendor": None,
    "material_request_id": None,
    "request_type": None,
    "mr_status": None,
    "rfq_id": None,
    "suppliers": None,
    "rfq_status": None,
    "purchase_order_id": None,
    "po_status": None,
    "purchase_receipt_id": None,
    "purchase_receipt_ids": None,
    "pr_status": None,
    "purchase_invoice_id": None,
    "pi_status": None,
    "charges": None,

    # Inventory skill
    "item_type": None,
    "has_batch": None,
    "has_serial": None,
    "warehouse_id": None,
    "source_warehouse_id": None,
    "target_warehouse_id": None,
    "stock_entry_type": None,
    "stock_entry_id": None,
    "warehouse_type": None,
    "account_id_inv": None,
    "pricing_rule_id": None,
    "min_qty": None,
    "max_qty": None,
    "discount_percentage": None,
    "reorder_level": None,
    "reorder_qty": None,

    # Shared across Phase 2
    "items": None,
    "payment_terms_id": None,

    # Manufacturing skill
    "bom_id": None,
    "work_order_id": None,
    "job_card_id": None,
    "production_plan_id": None,
    "quantity": None,
    "produced_qty": None,
    "for_quantity": None,
    "completed_qty": None,
    "operations": None,
    "routing_id": None,
    "operation_id": None,
    "workstation_id": None,
    "hour_rate": None,
    "time_in_mins": None,
    "actual_time_in_mins": None,
    "workstation_type": None,
    "working_hours_per_day": None,
    "production_capacity": None,
    "holiday_list_id": None,
    "planned_start_date": None,
    "planned_end_date": None,
    "wip_warehouse_id": None,
    "service_item_id": None,
    "supplier_warehouse_id": None,
    "planning_horizon_days": None,
    "is_active": None,
    "uom": None,
    "status": None,

    # HR skill
    "employee_id": None,
    "department_id": None,
    "designation_id": None,
    "employee_grade_id": None,
    "leave_type_id": None,
    "leave_application_id": None,
    "expense_claim_id": None,
    "payroll_cost_center_id": None,
    "salary_structure_id": None,
    "leave_policy_id": None,
    "shift_id": None,
    "attendance_device_id": None,
    "first_name": None,
    "last_name": None,
    "date_of_birth": None,
    "gender": None,
    "date_of_joining": None,
    "date_of_exit": None,
    "employment_type": None,
    "branch": None,
    "reporting_to": None,
    "company_email": None,
    "personal_email": None,
    "cell_phone": None,
    "emergency_contact": None,
    "bank_details": None,
    "federal_filing_status": None,
    "w4_allowances": None,
    "w4_additional_withholding": None,
    "state_filing_status": None,
    "state_withholding_allowances": None,
    "employee_401k_rate": None,
    "hsa_contribution": None,
    "is_exempt_from_fica": None,
    "max_days_allowed": None,
    "is_paid_leave": None,
    "is_carry_forward": None,
    "max_carry_forward_days": None,
    "is_compensatory": None,
    "applicable_after_days": None,
    "total_leaves": None,
    "half_day": None,
    "half_day_date": None,
    "approved_by": None,
    "date": None,
    "shift": None,
    "check_in_time": None,
    "check_out_time": None,
    "working_hours": None,
    "late_entry": None,
    "early_exit": None,
    "source": None,
    "holidays": None,
    "expense_date": None,
    "event_type": None,
    "event_date": None,
    "details": None,
    "old_values": None,
    "new_values": None,

    # Payroll skill
    "salary_component_id": None,
    "salary_assignment_id": None,
    "salary_slip_id": None,
    "payroll_run_id": None,
    "component_type": None,
    "is_tax_applicable": None,
    "is_statutory": None,
    "is_pre_tax": None,
    "variable_based_on_taxable_salary": None,
    "depends_on_payment_days": None,
    "gl_account_id": None,
    "payroll_frequency": None,
    "components": None,
    "base_amount": None,
    "period_start": None,
    "period_end": None,
    "tax_jurisdiction": None,
    "filing_status": None,
    "state_code": None,
    "standard_deduction": None,
    "rates": None,
    "tax_year": None,
    "ss_wage_base": None,
    "ss_employee_rate": None,
    "ss_employer_rate": None,
    "medicare_employee_rate": None,
    "medicare_employer_rate": None,
    "additional_medicare_threshold": None,
    "additional_medicare_rate": None,
    "wage_base": None,
    "rate": None,
    "employer_rate_override": None,
    "garnishment_id": None,
    "order_number": None,
    "creditor_name": None,
    "garnishment_type": None,
    "amount_or_percentage": None,
    "is_percentage": False,
    "total_owed": None,

    # CRM skill
    "lead_id": None,
    "opportunity_id": None,
    "campaign_id": None,
    "activity_id": None,
    "lead_name": None,
    "company_name": None,
    "email": None,
    "phone": None,
    "territory": None,
    "industry": None,
    "assigned_to": None,
    "notes": None,
    "opportunity_name": None,
    "opportunity_type": None,
    "expected_closing_date": None,
    "probability": None,
    "expected_revenue": None,
    "stage": None,
    "lost_reason": None,
    "next_follow_up_date": None,
    "campaign_type": None,
    "budget": None,
    "actual_spend": None,
    "activity_type": None,
    "subject": None,
    "activity_date": None,
    "created_by": None,
    "next_action_date": None,

    # Support skill
    "issue_id": None,
    "issue_type": None,
    "resolution_notes": None,
    "sla_id": None,
    "priorities": None,
    "comment": None,
    "comment_by": None,
    "is_internal": None,
    "serial_number_id": None,
    "warranty_claim_id": None,
    "warranty_expiry_date": None,
    "complaint_description": None,
    "resolution": None,
    "resolution_date": None,
    "cost": None,
    "schedule_id": None,
    "schedule_frequency": None,
    "visit_date": None,
    "completed_by": None,
    "observations": None,
    "work_done": None,

    # Billing skill
    "meter_id": None,
    "rate_plan_id": None,
    "billing_period_id": None,
    "meter_type": None,
    "unit": None,
    "install_date": None,
    "address": None,
    "reading_date": None,
    "reading_value": None,
    "reading_type": None,
    "estimated_reason": None,
    "idempotency_key": None,
    "events": None,
    "billing_model": None,
    "tiers": None,
    "base_charge": None,
    "base_charge_period": None,
    "minimum_charge": None,
    "minimum_commitment": None,
    "overage_rate": None,
    "service_type": None,
    "consumption": None,
    "billing_date": None,
    "billing_period_ids": None,
    "adjustment_type": None,
    "valid_until": None,

    # AI Engine skill
    "anomaly_id": None,
    "context_id": None,
    "severity": None,
    "horizon_days": None,
    "scenario_type": None,
    "assumptions": None,
    "rule_text": None,
    "action_type": None,
    "action_data": None,
    "pattern": None,
    "context_data": None,
    "decision_type": None,
    "options": None,
    "action_name": None,
    "result": None,
    "min_strength": None,

    # Analytics skill
    "group_by": "account",
    "metric": None,
    "metrics": None,

    # Projects skill
    "task_id": None,
    "milestone_id": None,
    "timesheet_id": None,
    "project_type": None,
    "billing_type": None,
    "estimated_cost": None,
    "actual_cost": None,
    "total_billed": None,
    "percent_complete": None,
    "estimated_hours": None,
    "actual_hours": None,
    "depends_on": None,
    "parent_task_id": None,
    "target_date": None,
    "completion_date": None,

    # Assets skill
    "asset_id": None,
    "asset_category_id": None,
    "depreciation_schedule_id": None,
    "maintenance_id": None,
    "depreciation_method": None,
    "useful_life_years": None,
    "asset_account_id": None,
    "depreciation_account_id": None,
    "accumulated_depreciation_account_id": None,
    "gross_value": None,
    "salvage_value": None,
    "purchase_date": None,
    "depreciation_start_date": None,
    "location": None,
    "custodian_employee_id": None,
    "movement_type": None,
    "movement_date": None,
    "from_location": None,
    "to_location": None,
    "from_employee_id": None,
    "to_employee_id": None,
    "maintenance_type": None,
    "scheduled_date": None,
    "actual_date": None,
    "performed_by": None,
    "next_due_date": None,
    "disposal_date": None,
    "disposal_method": None,
    "sale_amount": None,
    "buyer_details": None,

    # Quality skill
    "quality_inspection_id": None,
    "non_conformance_id": None,
    "quality_goal_id": None,
    "batch_id": None,
    "inspection_type": None,
    "parameters": None,
    "inspection_date": None,
    "inspected_by": None,
    "sample_size": None,
    "reference_type": None,
    "reference_id": None,
    "remarks": None,
    "readings": None,
    "root_cause": None,
    "corrective_action": None,
    "preventive_action": None,
    "responsible_employee_id": None,
    "measurable": None,
    "current_value": None,
    "target_value": None,
    "monitoring_frequency": None,
    "review_date": None,
}


_stdout_lock = threading.Lock()


def _call_action(skill_name, action_name, conn, **kwargs):
    """Call an action function from a named skill.

    Builds a Namespace with all possible args defaulted, then overrides
    with kwargs. Captures stdout and catches SystemExit.
    Returns the parsed JSON response dict.

    Thread-safe: uses a lock around sys.stdout redirection to prevent
    interleaving when called from multiple threads.
    """
    actions = _get_skill_actions(skill_name)
    if action_name not in actions:
        raise KeyError(f"Action '{action_name}' not found in {skill_name}")
    action_fn = actions[action_name]

    merged = {**_DEFAULT_ARGS, **kwargs}
    args = argparse.Namespace(**merged)

    captured = io.StringIO()
    with _stdout_lock:
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            action_fn(conn, args)
        except SystemExit:
            pass
        finally:
            sys.stdout = old_stdout

    output = captured.getvalue()
    return json.loads(output)


# ---------------------------------------------------------------------------
# init_db helper
# ---------------------------------------------------------------------------

_LOCAL_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../"))
_SERVER_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "erpclaw-setup", "scripts")
if os.path.exists(os.path.join(_LOCAL_ROOT, "init_db.py")):
    PROJECT_ROOT = _LOCAL_ROOT
elif os.path.exists(os.path.join(_SERVER_ROOT, "init_db.py")):
    PROJECT_ROOT = _SERVER_ROOT
else:
    PROJECT_ROOT = _LOCAL_ROOT


def _run_init_db(db_path: str):
    """Execute init_db.py to create all tables."""
    spec = importlib.util.spec_from_file_location(
        "init_db", os.path.join(PROJECT_ROOT, "init_db.py")
    )
    init_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_db)
    init_db.init_db(db_path)


# ---------------------------------------------------------------------------
# Data creation helpers
# ---------------------------------------------------------------------------

def create_test_company(conn, name="Test Company", abbr="TC"):
    """Insert a test company. Returns company_id."""
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month)
           VALUES (?, ?, ?, 'USD', 'United States', 1)""",
        (cid, name, abbr),
    )
    conn.commit()
    return cid


def create_test_fiscal_year(conn, company_id, name="FY 2026",
                             start_date="2026-01-01", end_date="2026-12-31"):
    """Insert a test fiscal year. Returns fiscal_year_id."""
    fy_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)
           VALUES (?, ?, ?, ?, ?)""",
        (fy_id, name, start_date, end_date, company_id),
    )
    conn.commit()
    return fy_id


def create_test_account(conn, company_id, name, root_type, account_type=None,
                         account_number=None, balance_direction=None,
                         is_group=0, parent_id=None):
    """Insert a test account. Returns account_id."""
    acct_id = str(uuid.uuid4())
    if balance_direction is None:
        balance_direction = "debit_normal"
        if root_type in ("liability", "equity", "income"):
            balance_direction = "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, account_number, parent_id, root_type,
           account_type, currency, is_group, balance_direction, company_id, depth)
           VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, 0)""",
        (acct_id, name, account_number, parent_id, root_type, account_type,
         is_group, balance_direction, company_id),
    )
    conn.commit()
    return acct_id


def seed_naming_series(conn, company_id, year=2026):
    """Seed naming series for all entity types. Returns count created."""
    from erpclaw_lib.naming import ENTITY_PREFIXES
    created = 0
    for entity_type, prefix in ENTITY_PREFIXES.items():
        year_prefix = f"{prefix}{year}-"
        existing = conn.execute(
            "SELECT id FROM naming_series WHERE entity_type = ? AND prefix = ? AND company_id = ?",
            (entity_type, year_prefix, company_id),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO naming_series (id, entity_type, prefix, current_value, company_id) VALUES (?, ?, ?, 0, ?)",
                (str(uuid.uuid4()), entity_type, year_prefix, company_id),
            )
            created += 1
    conn.commit()
    return created


def create_test_cost_center(conn, company_id, name="Main"):
    """Insert a test cost center. Returns cost_center_id."""
    cc_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group)
           VALUES (?, ?, ?, 0)""",
        (cc_id, name, company_id),
    )
    conn.commit()
    return cc_id


def create_test_customer(conn, company_id, name="Test Customer"):
    """Insert a test customer. Returns customer_id."""
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, customer_type, territory, company_id)
           VALUES (?, ?, 'company', 'United States', ?)""",
        (cid, name, company_id),
    )
    conn.commit()
    return cid


def create_test_supplier(conn, company_id, name="Test Supplier"):
    """Insert a test supplier. Returns supplier_id."""
    sid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO supplier (id, name, supplier_type, company_id)
           VALUES (?, ?, 'company', ?)""",
        (sid, name, company_id),
    )
    conn.commit()
    return sid


def create_test_item(conn, item_code="SKU-001", item_name="Widget A",
                     item_type="stock", stock_uom="Each",
                     valuation_method="moving_average", standard_rate="25.00"):
    """Insert a test item. Returns item_id."""
    item_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO item (id, item_code, item_name, item_type, stock_uom,
           valuation_method, standard_rate, has_batch, has_serial, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'active')""",
        (item_id, item_code, item_name, item_type, stock_uom,
         valuation_method, standard_rate),
    )
    conn.commit()
    return item_id


def create_test_warehouse(conn, company_id, name="Main Warehouse",
                          warehouse_type="stores", account_id=None):
    """Insert a test warehouse. Returns warehouse_id."""
    wh_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO warehouse (id, name, warehouse_type, account_id,
           company_id, is_group)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (wh_id, name, warehouse_type, account_id, company_id),
    )
    conn.commit()
    return wh_id


def create_test_tax_template(conn, company_id, name="Sales Tax 8%",
                             tax_type="sales"):
    """Insert a tax template with a single 8% line. Returns (template_id, tax_account_id)."""
    template_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO tax_template (id, name, tax_type, company_id)
           VALUES (?, ?, ?, ?)""",
        (template_id, name, tax_type, company_id),
    )
    acct_type = "tax"
    acct_name = "Sales Tax Payable" if tax_type == "sales" else "Input Tax"
    root_type = "liability" if tax_type == "sales" else "asset"
    acct_num = "2100" if tax_type == "sales" else "1500"
    tax_account_id = create_test_account(
        conn, company_id, acct_name, root_type,
        account_type=acct_type, account_number=acct_num,
    )
    line_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO tax_template_line (id, tax_template_id, tax_account_id,
           charge_type, rate, add_deduct, row_order)
           VALUES (?, ?, ?, 'on_net_total', '8.00', 'add', 1)""",
        (line_id, template_id, tax_account_id),
    )
    conn.commit()
    return template_id, tax_account_id


def seed_stock_for_item(conn, item_id, warehouse_id, qty="100", rate="25.00"):
    """Insert a stock ledger entry to seed stock for testing."""
    sle_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stock_ledger_entry
           (id, item_id, warehouse_id, posting_date, posting_time,
            actual_qty, valuation_rate, qty_after_transaction,
            stock_value, stock_value_difference,
            voucher_type, voucher_id, is_cancelled)
           VALUES (?, ?, ?, '2026-01-01', '00:00:00',
                   ?, ?, ?, ?, ?,
                   'stock_entry', ?, 0)""",
        (sle_id, item_id, warehouse_id,
         qty, rate, qty, str(float(qty) * float(rate)),
         str(float(qty) * float(rate)),
         str(uuid.uuid4())),
    )
    conn.commit()


def setup_phase2_environment(conn):
    """Create a complete environment for Phase 2 cross-skill tests.
    Sets up company, FY, naming series, accounts, items, warehouses,
    customer, supplier, and tax templates.
    Returns dict with all IDs needed.
    """
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)

    # Accounts
    receivable = create_test_account(conn, cid, "Accounts Receivable", "asset",
                                     account_type="receivable", account_number="1200")
    payable = create_test_account(conn, cid, "Accounts Payable", "liability",
                                  account_type="payable", account_number="2000")
    income = create_test_account(conn, cid, "Sales Revenue", "income",
                                 account_type="revenue", account_number="4000")
    expense = create_test_account(conn, cid, "Purchase Expense", "expense",
                                  account_type="expense", account_number="5000")
    cogs = create_test_account(conn, cid, "Cost of Goods Sold", "expense",
                               account_type="cost_of_goods_sold", account_number="5100")
    stock_in_hand = create_test_account(conn, cid, "Stock In Hand", "asset",
                                        account_type="stock", account_number="1400")
    stock_received = create_test_account(conn, cid, "Stock Received Not Billed", "liability",
                                         account_type="stock_received_not_billed",
                                         account_number="2200")
    stock_adjustment = create_test_account(conn, cid, "Stock Adjustment", "expense",
                                           account_type="stock_adjustment",
                                           account_number="5200")
    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")

    # Set company defaults
    conn.execute(
        """UPDATE company SET
           default_receivable_account_id = ?,
           default_payable_account_id = ?,
           default_income_account_id = ?
           WHERE id = ?""",
        (receivable, payable, income, cid),
    )
    conn.commit()

    # Item and warehouse
    item_id = create_test_item(conn)
    wh_id = create_test_warehouse(conn, cid, "Main Warehouse",
                                  account_id=stock_in_hand)

    # Seed stock for selling tests
    seed_stock_for_item(conn, item_id, wh_id, qty="200", rate="25.00")

    # Customer and supplier
    customer_id = create_test_customer(conn, cid)
    supplier_id = create_test_supplier(conn, cid)

    # Tax templates
    sales_tax_id, sales_tax_acct = create_test_tax_template(
        conn, cid, "Sales Tax 8%", "sales")
    purchase_tax_id, purchase_tax_acct = create_test_tax_template(
        conn, cid, "Purchase Tax 8%", "purchase")

    return {
        "company_id": cid,
        "fy_id": fy_id,
        "cost_center_id": cc,
        "receivable_id": receivable,
        "payable_id": payable,
        "income_id": income,
        "expense_id": expense,
        "cogs_id": cogs,
        "stock_in_hand_id": stock_in_hand,
        "stock_received_id": stock_received,
        "stock_adjustment_id": stock_adjustment,
        "bank_id": bank,
        "item_id": item_id,
        "warehouse_id": wh_id,
        "customer_id": customer_id,
        "supplier_id": supplier_id,
        "sales_tax_id": sales_tax_id,
        "sales_tax_acct": sales_tax_acct,
        "purchase_tax_id": purchase_tax_id,
        "purchase_tax_acct": purchase_tax_acct,
    }
