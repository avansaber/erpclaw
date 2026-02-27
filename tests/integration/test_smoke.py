"""Pre-deploy gate smoke tests: 15 critical-path tests that MUST pass.

Each test exercises a single end-to-end business flow across the ERPClaw
skill suite.  They are intentionally concise -- full coverage lives in the
T2 scenario tests.

All tests carry the ``@pytest.mark.smoke`` marker so they can be selected
with ``pytest -m smoke``.
"""
import json
import uuid
from decimal import Decimal

import pytest

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_account,
    create_test_cost_center,
    create_test_customer,
    create_test_supplier,
    create_test_item,
    create_test_warehouse,
    seed_stock_for_item,
    setup_phase2_environment,
)


# ---------------------------------------------------------------------------
# Connection wrapper (CRM, billing, support skills set conn.company_id)
# ---------------------------------------------------------------------------

class _ConnectionWrapper:
    """Thin wrapper around sqlite3.Connection supporting arbitrary attrs."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# Billing skill needs "meter" in ENTITY_PREFIXES
# ---------------------------------------------------------------------------
try:
    from erpclaw_lib.naming import ENTITY_PREFIXES
    if "meter" not in ENTITY_PREFIXES:
        ENTITY_PREFIXES["meter"] = "MTR-"
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _items_json(env, qty="10", rate="50.00"):
    """Standard selling items JSON payload."""
    return json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])


def _buying_items_json(env, qty="10", rate="50.00"):
    """Standard buying items JSON payload."""
    return json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])


def _set_default_expense_account(conn, company_id, expense_account_id):
    """Set the default expense account on the company."""
    conn.execute(
        "UPDATE company SET default_expense_account_id = ? WHERE id = ?",
        (expense_account_id, company_id),
    )
    conn.commit()


# ============================================================================
# 1. Company setup + CoA + Fiscal Year
# ============================================================================

@pytest.mark.smoke
def test_smoke_company_setup_coa_fy(fresh_db):
    """Create company via setup skill, setup CoA, add FY -- verify all exist."""
    conn = fresh_db

    # Create company
    r = _call_action("erpclaw-setup", "setup-company", conn,
                     name="Smoke Co", abbr="SMK",
                     currency="USD", country="United States",
                     fiscal_year_start_month="1")
    assert r["status"] == "ok"
    cid = r["company_id"]

    # Setup CoA
    r = _call_action("erpclaw-gl", "setup-chart-of-accounts", conn,
                     company_id=cid, template="us_gaap")
    assert r["status"] == "ok"
    accounts_created = r.get("accounts_created", 0)
    assert accounts_created > 0

    # Add FY
    r = _call_action("erpclaw-gl", "add-fiscal-year", conn,
                     company_id=cid, name="FY 2026",
                     start_date="2026-01-01", end_date="2026-12-31")
    assert r["status"] == "ok"

    # Verify company
    row = conn.execute("SELECT * FROM company WHERE id = ?", (cid,)).fetchone()
    assert row is not None

    # Verify accounts
    acct_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM account WHERE company_id = ?", (cid,)
    ).fetchone()["cnt"]
    assert acct_count > 0

    # Verify FY
    fy_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM fiscal_year WHERE company_id = ?", (cid,)
    ).fetchone()["cnt"]
    assert fy_count >= 1


# ============================================================================
# 2. Order to Cash (O2C)
# ============================================================================

@pytest.mark.smoke
def test_smoke_order_to_cash(fresh_db):
    """Full O2C: quotation -> SO -> DN -> SI -> payment -> GL balanced."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    items_j = _items_json(env, qty="5", rate="50.00")

    # Quotation
    r = _call_action("erpclaw-selling", "add-quotation", conn,
                     customer_id=env["customer_id"],
                     posting_date="2026-06-01", items=items_j,
                     company_id=env["company_id"])
    assert r["status"] == "ok"
    q_id = r["quotation_id"]

    r = _call_action("erpclaw-selling", "submit-quotation", conn,
                     quotation_id=q_id)
    assert r["status"] == "ok"

    # Convert to SO
    r = _call_action("erpclaw-selling", "convert-quotation-to-so", conn,
                     quotation_id=q_id, delivery_date="2026-07-01")
    assert r["status"] == "ok"
    so_id = r["sales_order_id"]

    # Set warehouse on SO items (quotation doesn't carry warehouse_id)
    conn.execute(
        "UPDATE sales_order_item SET warehouse_id = ? WHERE sales_order_id = ?",
        (env["warehouse_id"], so_id))
    conn.commit()

    # Submit SO
    r = _call_action("erpclaw-selling", "submit-sales-order", conn,
                     sales_order_id=so_id)
    assert r["status"] == "ok"

    # Delivery Note
    r = _call_action("erpclaw-selling", "create-delivery-note", conn,
                     sales_order_id=so_id, posting_date="2026-07-01")
    assert r["status"] == "ok"
    dn_id = r["delivery_note_id"]

    r = _call_action("erpclaw-selling", "submit-delivery-note", conn,
                     delivery_note_id=dn_id)
    assert r["status"] == "ok"

    # Sales Invoice
    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                     sales_order_id=so_id, posting_date="2026-07-02")
    assert r["status"] == "ok"
    si_id = r["sales_invoice_id"]

    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                     sales_invoice_id=si_id)
    assert r["status"] == "ok"

    grand_total = conn.execute(
        "SELECT grand_total FROM sales_invoice WHERE id = ?", (si_id,)
    ).fetchone()["grand_total"]

    # Payment
    r = _call_action("erpclaw-payments", "add-payment", conn,
                     company_id=env["company_id"],
                     payment_type="receive",
                     posting_date="2026-07-10",
                     party_type="customer",
                     party_id=env["customer_id"],
                     paid_from_account=env["receivable_id"],
                     paid_to_account=env["bank_id"],
                     paid_amount=grand_total)
    assert r["status"] == "ok"
    pe_id = r["payment_entry_id"]

    r = _call_action("erpclaw-payments", "submit-payment", conn,
                     payment_entry_id=pe_id)
    assert r["status"] == "ok"

    # GL balanced
    gl_rows = conn.execute(
        "SELECT * FROM gl_entry WHERE is_cancelled = 0"
    ).fetchall()
    total_dr = sum(Decimal(g["debit"]) for g in gl_rows)
    total_cr = sum(Decimal(g["credit"]) for g in gl_rows)
    assert abs(total_dr - total_cr) < Decimal("0.01")


# ============================================================================
# 3. Procure to Pay (P2P)
# ============================================================================

@pytest.mark.smoke
def test_smoke_procure_to_pay(fresh_db):
    """Full P2P: PO -> receipt -> PI -> payment -> GL balanced."""
    conn = fresh_db
    env = setup_phase2_environment(conn)
    _set_default_expense_account(conn, env["company_id"], env["expense_id"])

    items_j = _buying_items_json(env, qty="10", rate="50.00")

    # PO
    r = _call_action("erpclaw-buying", "add-purchase-order", conn,
                     supplier_id=env["supplier_id"],
                     company_id=env["company_id"],
                     items=items_j, posting_date="2026-03-01")
    assert r["status"] == "ok"
    po_id = r["purchase_order_id"]

    r = _call_action("erpclaw-buying", "submit-purchase-order", conn,
                     purchase_order_id=po_id)
    assert r["status"] == "ok"

    # Purchase Receipt
    r = _call_action("erpclaw-buying", "create-purchase-receipt", conn,
                     purchase_order_id=po_id, posting_date="2026-03-05")
    assert r["status"] == "ok"
    pr_id = r["purchase_receipt_id"]

    r = _call_action("erpclaw-buying", "submit-purchase-receipt", conn,
                     purchase_receipt_id=pr_id)
    assert r["status"] == "ok"

    # Purchase Invoice
    r = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                     purchase_order_id=po_id,
                     posting_date="2026-03-10", due_date="2026-04-10")
    assert r["status"] == "ok"
    pi_id = r["purchase_invoice_id"]
    grand_total = r["grand_total"]

    r = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                     purchase_invoice_id=pi_id)
    assert r["status"] == "ok"

    # Payment (pay type)
    r = _call_action("erpclaw-payments", "add-payment", conn,
                     company_id=env["company_id"],
                     payment_type="pay",
                     posting_date="2026-03-20",
                     party_type="supplier",
                     party_id=env["supplier_id"],
                     paid_from_account=env["bank_id"],
                     paid_to_account=env["payable_id"],
                     paid_amount=grand_total)
    assert r["status"] == "ok"
    pe_id = r["payment_entry_id"]

    r = _call_action("erpclaw-payments", "submit-payment", conn,
                     payment_entry_id=pe_id)
    assert r["status"] == "ok"

    # GL balanced
    r = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                     company_id=env["company_id"])
    assert r["status"] == "ok"
    assert r["balanced"] is True


# ============================================================================
# 4. Trial Balance
# ============================================================================

@pytest.mark.smoke
def test_smoke_trial_balance(fresh_db):
    """Submit a JE with known amounts, verify trial balance totals match."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)
    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                  account_type="revenue", account_number="4000")

    lines = json.dumps([
        {"account_id": bank, "debit": "5000.00", "credit": "0"},
        {"account_id": revenue, "debit": "0", "credit": "5000.00",
         "cost_center_id": cc},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=cid, posting_date="2026-06-15", lines=lines)
    assert r["status"] == "ok"
    je_id = r["journal_entry_id"]

    r = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                     journal_entry_id=je_id, company_id=cid)
    assert r["status"] == "ok"

    # Trial balance
    r = _call_action("erpclaw-reports", "trial-balance", conn,
                     company_id=cid, to_date="2026-12-31")
    assert r["status"] == "ok"
    assert Decimal(r["total_debit"]) == Decimal(r["total_credit"])
    assert Decimal(r["total_debit"]) >= Decimal("5000.00")


# ============================================================================
# 5. Payroll Cycle
# ============================================================================

@pytest.mark.smoke
def test_smoke_payroll_cycle(fresh_db):
    """Employee -> salary structure -> payroll run -> submit -> GL created."""
    conn = fresh_db
    cid = create_test_company(conn, name="Payroll Co", abbr="PY")
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)

    # Accounts
    salary_exp = create_test_account(conn, cid, "Salary Expense", "expense",
                                     account_type="expense", account_number="6100")
    payroll_payable = create_test_account(conn, cid, "Payroll Payable", "liability",
                                          account_type="payable", account_number="2300")
    fed_tax = create_test_account(conn, cid, "Federal Income Tax Withheld", "liability",
                                  account_type="tax", account_number="2310")
    ss_payable = create_test_account(conn, cid, "Social Security Payable", "liability",
                                     account_type="tax", account_number="2320")
    medicare_payable = create_test_account(conn, cid, "Medicare Payable", "liability",
                                           account_type="tax", account_number="2330")
    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")

    # Department + designation
    r = _call_action("erpclaw-hr", "add-department", conn,
                     name="Engineering", company_id=cid)
    assert r["status"] == "ok"
    dept_id = r["department_id"]

    r = _call_action("erpclaw-hr", "add-designation", conn,
                     name="Engineer")
    assert r["status"] == "ok"
    desig_id = r["designation_id"]

    # Employee
    r = _call_action("erpclaw-hr", "add-employee", conn,
                     first_name="Jane", last_name="Doe",
                     date_of_birth="1990-01-01", gender="female",
                     date_of_joining="2026-01-01",
                     employment_type="full_time",
                     company_id=cid, department_id=dept_id,
                     designation_id=desig_id)
    assert r["status"] == "ok"
    emp_id = r["employee_id"]

    # Salary component
    r = _call_action("erpclaw-payroll", "add-salary-component", conn,
                     name="Basic Salary", component_type="earning",
                     is_tax_applicable="1")
    assert r["status"] == "ok"
    basic_id = r["salary_component_id"]

    # Salary structure
    components = json.dumps([{
        "salary_component_id": basic_id,
        "amount": "0",
        "sort_order": 0,
    }])
    r = _call_action("erpclaw-payroll", "add-salary-structure", conn,
                     name="Standard", payroll_frequency="monthly",
                     company_id=cid, components=components)
    assert r["status"] == "ok"
    struct_id = r["salary_structure_id"]

    # Assignment
    r = _call_action("erpclaw-payroll", "add-salary-assignment", conn,
                     employee_id=emp_id, salary_structure_id=struct_id,
                     base_amount="5000.00", effective_from="2026-01-01")
    assert r["status"] == "ok"

    # FICA config
    r = _call_action("erpclaw-payroll", "update-fica-config", conn,
                     tax_year="2026", ss_wage_base="168600",
                     ss_employee_rate="6.2", ss_employer_rate="6.2",
                     medicare_employee_rate="1.45", medicare_employer_rate="1.45",
                     additional_medicare_threshold="200000",
                     additional_medicare_rate="0.9")
    assert r["status"] == "ok"

    # Income tax slab
    rates = json.dumps([
        {"from_amount": "0", "to_amount": "11600", "rate": "10"},
        {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
        {"from_amount": "47150", "to_amount": "100525", "rate": "22"},
    ])
    r = _call_action("erpclaw-payroll", "add-income-tax-slab", conn,
                     name="Federal 2026 Single", tax_jurisdiction="federal",
                     filing_status="single", effective_from="2026-01-01",
                     standard_deduction="14600", rates=rates)
    assert r["status"] == "ok"

    # Payroll run
    r = _call_action("erpclaw-payroll", "create-payroll-run", conn,
                     company_id=cid, period_start="2026-01-01",
                     period_end="2026-01-31", payroll_frequency="monthly")
    assert r["status"] == "ok"
    run_id = r["payroll_run_id"]

    r = _call_action("erpclaw-payroll", "generate-salary-slips", conn,
                     payroll_run_id=run_id)
    assert r["status"] == "ok"
    assert r["slips_generated"] == 1

    r = _call_action("erpclaw-payroll", "submit-payroll-run", conn,
                     payroll_run_id=run_id, cost_center_id=cc)
    assert r["status"] == "ok"
    assert r["gl_entries"] >= 2

    # GL balanced
    gl_rows = conn.execute(
        "SELECT * FROM gl_entry WHERE is_cancelled = 0"
    ).fetchall()
    total_dr = sum(Decimal(g["debit"]) for g in gl_rows)
    total_cr = sum(Decimal(g["credit"]) for g in gl_rows)
    assert abs(total_dr - total_cr) < Decimal("0.02")


# ============================================================================
# 6. Manufacturing BOM + Work Order
# ============================================================================

@pytest.mark.smoke
def test_smoke_manufacturing_bom_wo(fresh_db):
    """BOM -> work order -> start -> transfer -> complete -> FG stock."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)

    stock_in_hand = create_test_account(conn, cid, "Stock In Hand", "asset",
                                        account_type="stock", account_number="1400")
    wip_acct = create_test_account(conn, cid, "WIP", "asset",
                                   account_type="stock", account_number="1410")

    stores_wh = create_test_warehouse(conn, cid, "Stores",
                                      account_id=stock_in_hand)
    wip_wh = create_test_warehouse(conn, cid, "WIP Warehouse",
                                   warehouse_type="transit", account_id=wip_acct)
    fg_wh = create_test_warehouse(conn, cid, "FG Warehouse",
                                  account_id=stock_in_hand)

    rm = create_test_item(conn, item_code="RM-S1", item_name="Steel",
                          stock_uom="Kg", standard_rate="10.00")
    fg = create_test_item(conn, item_code="FG-S1", item_name="Widget",
                          standard_rate="0")

    seed_stock_for_item(conn, rm, stores_wh, qty="500", rate="10.00")

    # BOM
    bom_items = json.dumps([{"item_id": rm, "quantity": "2", "rate": "10.00"}])
    r = _call_action("erpclaw-manufacturing", "add-bom", conn,
                     item_id=fg, items=bom_items, company_id=cid, quantity="1")
    assert r["status"] == "ok"
    bom_id = r["bom_id"]

    # Work order for 10 units
    r = _call_action("erpclaw-manufacturing", "add-work-order", conn,
                     bom_id=bom_id, quantity="10", company_id=cid,
                     planned_start_date="2026-03-01",
                     source_warehouse_id=stores_wh,
                     target_warehouse_id=fg_wh,
                     wip_warehouse_id=wip_wh)
    assert r["status"] == "ok"
    wo_id = r["work_order_id"]

    # Start
    r = _call_action("erpclaw-manufacturing", "start-work-order", conn,
                     work_order_id=wo_id)
    assert r["status"] == "ok"

    # Transfer materials (10 FG needs 20 RM)
    transfer_items = json.dumps([
        {"item_id": rm, "qty": "20", "warehouse_id": stores_wh},
    ])
    r = _call_action("erpclaw-manufacturing", "transfer-materials", conn,
                     work_order_id=wo_id, items=transfer_items,
                     posting_date="2026-03-02")
    assert r["status"] == "ok"

    # Complete
    r = _call_action("erpclaw-manufacturing", "complete-work-order", conn,
                     work_order_id=wo_id, posting_date="2026-03-05")
    assert r["status"] == "ok"
    assert Decimal(r["produced_qty"]) == Decimal("10.00")


# ============================================================================
# 7. Asset Depreciation
# ============================================================================

@pytest.mark.smoke
def test_smoke_asset_depreciation(fresh_db):
    """Register asset -> generate depreciation schedule -> post -> GL."""
    conn = fresh_db
    cid = create_test_company(conn, name="Asset Co", abbr="AS")
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)

    asset_acct = create_test_account(conn, cid, "Equipment", "asset",
                                     account_type="fixed_asset",
                                     account_number="1500")
    dep_expense = create_test_account(conn, cid, "Depreciation Expense", "expense",
                                      account_type="expense", account_number="5400")
    accum_dep = create_test_account(conn, cid, "Accumulated Depreciation", "asset",
                                    account_type="accumulated_depreciation",
                                    account_number="1510")

    # Category
    r = _call_action("erpclaw-assets", "add-asset-category", conn,
                     name="Office Equipment", company_id=cid,
                     depreciation_method="straight_line",
                     useful_life_years="5",
                     depreciation_account_id=dep_expense,
                     accumulated_depreciation_account_id=accum_dep)
    assert r["status"] == "ok"
    cat_id = r["asset_category_id"]

    # Asset
    r = _call_action("erpclaw-assets", "add-asset", conn,
                     name="MacBook Pro", asset_category_id=cat_id,
                     company_id=cid, gross_value="2500.00",
                     purchase_date="2026-01-01",
                     asset_account_id=asset_acct,
                     salvage_value="250.00",
                     useful_life_years="5",
                     depreciation_start_date="2026-02-01")
    assert r["status"] == "ok"
    asset_id = r["asset_id"]

    # Submit asset (must be submitted/in_use before posting depreciation)
    r = _call_action("erpclaw-assets", "update-asset", conn,
                     asset_id=asset_id, status="submitted")
    assert r["status"] == "ok"

    # Generate schedule
    r = _call_action("erpclaw-assets", "generate-depreciation-schedule", conn,
                     asset_id=asset_id)
    assert r["status"] == "ok"
    assert r["entries_generated"] > 0

    # Post first depreciation
    r = _call_action("erpclaw-assets", "post-depreciation", conn,
                     asset_id=asset_id, posting_date="2026-02-28")
    assert r["status"] == "ok"

    # Verify GL entries created
    gl_rows = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'depreciation_entry' AND is_cancelled = 0"""
    ).fetchall()
    assert len(gl_rows) >= 2
    total_dr = sum(Decimal(g["debit"]) for g in gl_rows)
    total_cr = sum(Decimal(g["credit"]) for g in gl_rows)
    assert abs(total_dr - total_cr) < Decimal("0.01")


# ============================================================================
# 8. CRM Lead to Opportunity Won
# ============================================================================

@pytest.mark.smoke
def test_smoke_crm_lead_to_opportunity(fresh_db):
    """Add lead -> qualify -> convert to opportunity -> mark won."""
    wrapped = _ConnectionWrapper(fresh_db)
    env = setup_phase2_environment(wrapped)
    cid = env["company_id"]

    # Lead
    r = _call_action("erpclaw-crm", "add-lead", wrapped,
                     company_id=cid, lead_name="Test Lead",
                     company_name="Acme", source="website")
    assert r["status"] == "ok"
    lead_id = r["lead"]["id"]

    # Qualify
    r = _call_action("erpclaw-crm", "update-lead", wrapped,
                     company_id=cid, lead_id=lead_id,
                     status="qualified")
    assert r["status"] == "ok"

    # Convert to opportunity
    r = _call_action("erpclaw-crm", "convert-lead-to-opportunity", wrapped,
                     company_id=cid, lead_id=lead_id,
                     opportunity_name="Acme Deal",
                     expected_revenue="50000.00", probability="60",
                     opportunity_type="sales")
    assert r["status"] == "ok"
    opp_id = r["opportunity"]["id"]
    assert r["opportunity"]["stage"] == "new"

    # Mark won
    r = _call_action("erpclaw-crm", "mark-opportunity-won", wrapped,
                     company_id=cid, opportunity_id=opp_id)
    assert r["status"] == "ok"
    assert r["opportunity"]["stage"] == "won"


# ============================================================================
# 9. Support Ticket Lifecycle
# ============================================================================

@pytest.mark.smoke
def test_smoke_support_ticket_lifecycle(fresh_db):
    """Add SLA -> create issue -> resolve issue -> verify resolved."""
    wrapped = _ConnectionWrapper(fresh_db)
    cid = create_test_company(wrapped)
    create_test_fiscal_year(wrapped, cid)
    seed_naming_series(wrapped, cid)
    customer_id = create_test_customer(wrapped, cid, name="Support Customer")

    # SLA
    priorities = json.dumps({
        "response_times": {"low": 24, "medium": 8, "high": 4, "critical": 1},
        "resolution_times": {"low": 72, "medium": 24, "high": 12, "critical": 4},
    })
    r = _call_action("erpclaw-support", "add-sla", wrapped,
                     name="Standard SLA", priorities=priorities,
                     is_default="1", company_id=cid)
    assert r["status"] == "ok"

    # Issue
    r = _call_action("erpclaw-support", "add-issue", wrapped,
                     subject="Server down",
                     customer_id=customer_id,
                     issue_type="bug", priority="high",
                     description="Production outage",
                     company_id=cid)
    assert r["status"] == "ok"
    issue_id = r["issue"]["id"]
    assert r["issue"]["status"] == "open"

    # First response (to set first_response_at)
    r = _call_action("erpclaw-support", "add-issue-comment", wrapped,
                     issue_id=issue_id,
                     comment="Looking into it.",
                     comment_by="employee", is_internal="0",
                     company_id=cid)
    assert r["status"] == "ok"

    # Resolve
    r = _call_action("erpclaw-support", "resolve-issue", wrapped,
                     issue_id=issue_id,
                     resolution_notes="Fixed the outage.",
                     company_id=cid)
    assert r["status"] == "ok"
    assert r["issue"]["status"] == "resolved"


# ============================================================================
# 10. Billing Meter to Invoice
# ============================================================================

@pytest.mark.smoke
def test_smoke_billing_meter_to_invoice(fresh_db):
    """Meter -> readings -> rate plan -> run billing -> generate invoices."""
    wrapped = _ConnectionWrapper(fresh_db)
    cid = create_test_company(wrapped, name="Billing Corp", abbr="BC")
    create_test_fiscal_year(wrapped, cid)
    seed_naming_series(wrapped, cid)

    receivable = create_test_account(wrapped, cid, "Accounts Receivable", "asset",
                                     account_type="receivable", account_number="1200")
    income = create_test_account(wrapped, cid, "Billing Revenue", "income",
                                 account_type="revenue", account_number="4000")
    wrapped.execute(
        "UPDATE company SET default_receivable_account_id = ?, default_income_account_id = ? WHERE id = ?",
        (receivable, income, cid))
    wrapped.commit()

    customer_id = create_test_customer(wrapped, cid, name="Utility Customer")

    # Rate plan (flat)
    tiers = json.dumps([{"tier_start": "0", "rate": "0.10"}])
    r = _call_action("erpclaw-billing", "add-rate-plan", wrapped,
                     name="Flat Plan", billing_model="flat",
                     tiers=tiers, base_charge="5.00")
    assert r["status"] == "ok"
    rp_id = r["rate_plan"]["id"]

    # Meter
    r = _call_action("erpclaw-billing", "add-meter", wrapped,
                     name="SP-Main", customer_id=customer_id,
                     meter_type="electricity", unit="kWh",
                     install_date="2026-01-01", rate_plan_id=rp_id)
    assert r["status"] == "ok"
    meter_id = r["meter"]["id"]

    # Readings
    r = _call_action("erpclaw-billing", "add-meter-reading", wrapped,
                     meter_id=meter_id, reading_date="2026-01-01",
                     reading_value="0", reading_type="actual")
    assert r["status"] == "ok"

    r = _call_action("erpclaw-billing", "add-meter-reading", wrapped,
                     meter_id=meter_id, reading_date="2026-01-31",
                     reading_value="300", reading_type="actual")
    assert r["status"] == "ok"

    # Run billing
    r = _call_action("erpclaw-billing", "run-billing", wrapped,
                     company_id=cid, billing_date="2026-01-31",
                     from_date="2026-01-01", to_date="2026-01-31")
    assert r["status"] == "ok"
    assert r["periods_created"] == 1
    bp_id = r["period_ids"][0]

    # Generate invoices
    r = _call_action("erpclaw-billing", "generate-invoices", wrapped,
                     billing_period_ids=json.dumps([bp_id]))
    assert r["status"] == "ok"
    assert r["invoiced"] == 1


# ============================================================================
# 11. AI Anomaly Detection
# ============================================================================

@pytest.mark.smoke
def test_smoke_ai_anomaly_detection(fresh_db):
    """Post GL data with round numbers, run anomaly detection, verify response."""
    conn = fresh_db
    cid = create_test_company(conn, name="AI Co", abbr="AI")
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)
    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                  account_type="revenue", account_number="4000")

    # Post some round-number JEs
    for date, amount in [("2026-01-15", "5000.00"), ("2026-02-15", "10000.00")]:
        lines = json.dumps([
            {"account_id": bank, "debit": amount, "credit": "0"},
            {"account_id": revenue, "debit": "0", "credit": amount,
             "cost_center_id": cc},
        ])
        r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                         company_id=cid, posting_date=date, lines=lines)
        assert r["status"] == "ok"
        r = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                         journal_entry_id=r["journal_entry_id"], company_id=cid)
        assert r["status"] == "ok"

    # Detect anomalies
    r = _call_action("erpclaw-ai-engine", "detect-anomalies", conn,
                     company_id=cid, from_date="2026-01-01",
                     to_date="2026-12-31")
    assert r["status"] == "ok"
    assert r["anomalies_detected"] > 0


# ============================================================================
# 12. Multi-Company Isolation
# ============================================================================

@pytest.mark.smoke
def test_smoke_multi_company_isolation(fresh_db):
    """Two companies: JE in A invisible to B, customers isolated."""
    conn = fresh_db

    # Company A
    r = _call_action("erpclaw-setup", "setup-company", conn,
                     name="Alpha Corp", abbr="AC",
                     currency="USD", country="United States",
                     fiscal_year_start_month="1")
    assert r["status"] == "ok"
    cid_a = r["company_id"]

    r = _call_action("erpclaw-gl", "add-fiscal-year", conn,
                     company_id=cid_a, name="FY-A 2026",
                     start_date="2026-01-01", end_date="2026-12-31")
    assert r["status"] == "ok"

    r = _call_action("erpclaw-gl", "seed-naming-series", conn,
                     company_id=cid_a)
    assert r["status"] == "ok"

    cc_a = create_test_cost_center(conn, cid_a, name="Main - AC")
    cash_a = create_test_account(conn, cid_a, "Cash - AC", "asset",
                                 account_type="bank", account_number="A1010")
    rev_a = create_test_account(conn, cid_a, "Revenue - AC", "income",
                                account_type="revenue", account_number="A4000")
    create_test_customer(conn, cid_a, name="Customer A")

    # Company B
    r = _call_action("erpclaw-setup", "setup-company", conn,
                     name="Beta Inc", abbr="BI",
                     currency="USD", country="United States",
                     fiscal_year_start_month="1")
    assert r["status"] == "ok"
    cid_b = r["company_id"]

    r = _call_action("erpclaw-gl", "add-fiscal-year", conn,
                     company_id=cid_b, name="FY-B 2026",
                     start_date="2026-01-01", end_date="2026-12-31")
    assert r["status"] == "ok"

    r = _call_action("erpclaw-gl", "seed-naming-series", conn,
                     company_id=cid_b)
    assert r["status"] == "ok"

    # Post JE in company A
    lines = json.dumps([
        {"account_id": cash_a, "debit": "10000.00", "credit": "0"},
        {"account_id": rev_a, "debit": "0", "credit": "10000.00",
         "cost_center_id": cc_a},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=cid_a, posting_date="2026-03-01", lines=lines)
    assert r["status"] == "ok"
    r = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                     journal_entry_id=r["journal_entry_id"], company_id=cid_a)
    assert r["status"] == "ok"

    # Verify GL only in A (gl_entry has no company_id -- join through account)
    gl_a = conn.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry ge
           JOIN account acc ON ge.account_id = acc.id
           WHERE acc.company_id = ? AND ge.is_cancelled = 0""",
        (cid_a,)).fetchone()["cnt"]
    gl_b = conn.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry ge
           JOIN account acc ON ge.account_id = acc.id
           WHERE acc.company_id = ? AND ge.is_cancelled = 0""",
        (cid_b,)).fetchone()["cnt"]
    assert gl_a > 0
    assert gl_b == 0

    # Verify customers isolated
    cust_b = conn.execute(
        "SELECT COUNT(*) as cnt FROM customer WHERE company_id = ?",
        (cid_b,)).fetchone()["cnt"]
    assert cust_b == 0


# ============================================================================
# 13. Credit Note GL Reversal
# ============================================================================

@pytest.mark.smoke
def test_smoke_credit_note_gl_reversal(fresh_db):
    """SI -> submit -> credit note -> submit -> verify reversal GL entries."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    items_j = _items_json(env, qty="5", rate="100.00")

    # Create and submit SI
    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                     customer_id=env["customer_id"],
                     posting_date="2026-06-01", items=items_j,
                     company_id=env["company_id"])
    assert r["status"] == "ok"
    si_id = r["sales_invoice_id"]

    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                     sales_invoice_id=si_id)
    assert r["status"] == "ok"

    # Create credit note (full)
    cn_items = json.dumps([{
        "item_id": env["item_id"], "qty": "5", "rate": "100.00",
    }])
    r = _call_action("erpclaw-selling", "create-credit-note", conn,
                     against_invoice_id=si_id, items=cn_items,
                     posting_date="2026-06-10", reason="Returned goods")
    assert r["status"] == "ok"
    cn_id = r["credit_note_id"]

    # Submit credit note
    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                     sales_invoice_id=cn_id)
    assert r["status"] == "ok"

    # Verify credit note GL
    cn_gl = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'credit_note' AND voucher_id = ?
             AND is_cancelled = 0""",
        (cn_id,)).fetchall()
    assert len(cn_gl) >= 2

    cn_dr = sum(Decimal(g["debit"]) for g in cn_gl)
    cn_cr = sum(Decimal(g["credit"]) for g in cn_gl)
    assert abs(cn_dr - cn_cr) < Decimal("0.01")

    # Receivable should be credited (opposite of original)
    recv_gl = [g for g in cn_gl if g["account_id"] == env["receivable_id"]]
    assert len(recv_gl) >= 1
    recv_credit = sum(Decimal(g["credit"]) for g in recv_gl)
    assert recv_credit == Decimal("500.00")


# ============================================================================
# 14. Intercompany Journal Entry
# ============================================================================

@pytest.mark.smoke
def test_smoke_intercompany_journal_entry(fresh_db):
    """Create JE with entry_type=inter_company, submit, GL balanced."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)
    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                  account_type="revenue", account_number="4000")

    lines = json.dumps([
        {"account_id": bank, "debit": "15000.00", "credit": "0"},
        {"account_id": revenue, "debit": "0", "credit": "15000.00",
         "cost_center_id": cc},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=cid, posting_date="2026-06-01",
                     entry_type="inter_company", lines=lines)
    assert r["status"] == "ok"
    je_id = r["journal_entry_id"]

    r = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                     journal_entry_id=je_id, company_id=cid)
    assert r["status"] == "ok"
    assert r["gl_entries_created"] == 2

    # GL balanced
    gl_rows = conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (je_id,)).fetchall()
    total_dr = sum(Decimal(g["debit"]) for g in gl_rows)
    total_cr = sum(Decimal(g["credit"]) for g in gl_rows)
    assert abs(total_dr - total_cr) < Decimal("0.01")


# ============================================================================
# 15. GL Integrity Check
# ============================================================================

@pytest.mark.smoke
def test_smoke_gl_integrity_check(fresh_db):
    """Submit a JE, then run check-gl-integrity, verify passes."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)
    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    expense = create_test_account(conn, cid, "Expense", "expense",
                                  account_type="expense", account_number="5000")

    lines = json.dumps([
        {"account_id": expense, "debit": "3000.00", "credit": "0",
         "cost_center_id": cc},
        {"account_id": bank, "debit": "0", "credit": "3000.00"},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                     company_id=cid, posting_date="2026-06-01", lines=lines)
    assert r["status"] == "ok"
    je_id = r["journal_entry_id"]

    r = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                     journal_entry_id=je_id, company_id=cid)
    assert r["status"] == "ok"

    # Integrity check
    r = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                     company_id=cid)
    assert r["status"] == "ok"
    assert r["balanced"] is True
