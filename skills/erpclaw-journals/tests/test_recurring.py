"""Tests for recurring journal template actions (V1).

12 tests:
- CRUD (4): add, update, list, get + delete
- Generates correct JE with GL (1)
- Idempotent — re-run doesn't duplicate (1)
- Monthly frequency date advancement (1)
- End date stops generation (1)
- Multiple templates in one run (1)
- Paused template skipped (1)
- Auto-submit + GL verification (1)
- Invariant — GL balance after process (1)
"""
import json
import os
import sys

import pytest

# Ensure scripts/ is importable
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from db_query import (
    add_recurring_template,
    update_recurring_template,
    list_recurring_templates,
    get_recurring_template,
    process_recurring,
    delete_recurring_template,
)
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def setup_data(fresh_db):
    """Company + FY + 2 accounts for recurring template tests."""
    conn = fresh_db
    company_id = create_test_company(conn, "Recurring Corp", "RC")
    create_test_fiscal_year(conn, company_id)
    debit_acct = create_test_account(conn, company_id, "Rent Expense", "expense",
                                     account_type="expense", account_number="5100")
    credit_acct = create_test_account(conn, company_id, "Bank", "asset",
                                      account_type="bank", account_number="1100")
    lines = json.dumps([
        {"account_id": debit_acct, "debit": "5000.00", "credit": "0.00"},
        {"account_id": credit_acct, "debit": "0.00", "credit": "5000.00"},
    ])
    return conn, company_id, debit_acct, credit_acct, lines


# ---------------------------------------------------------------------------
# 1. CRUD: Add recurring template
# ---------------------------------------------------------------------------

def test_add_recurring_template(setup_data):
    conn, company_id, _, _, lines = setup_data
    r = _call_action(add_recurring_template, conn,
                     company_id=company_id,
                     template_name="Monthly Rent",
                     frequency="monthly",
                     start_date="2026-03-01",
                     lines=lines)
    assert r.get("status") == "ok"
    assert r["template_id"]
    assert r["next_run_date"] == "2026-03-01"


# ---------------------------------------------------------------------------
# 2. CRUD: Update recurring template
# ---------------------------------------------------------------------------

def test_update_recurring_template(setup_data):
    conn, company_id, _, _, lines = setup_data
    r = _call_action(add_recurring_template, conn,
                     company_id=company_id,
                     template_name="Monthly Rent",
                     frequency="monthly",
                     start_date="2026-03-01",
                     lines=lines)
    template_id = r["template_id"]

    r2 = _call_action(update_recurring_template, conn,
                      template_id=template_id,
                      template_name="Monthly Office Rent",
                      frequency="quarterly")
    assert r2.get("status") == "ok"
    assert "name" in r2["updated_fields"]
    assert "frequency" in r2["updated_fields"]

    # Verify via get
    r3 = _call_action(get_recurring_template, conn, template_id=template_id)
    assert r3["name"] == "Monthly Office Rent"
    assert r3["frequency"] == "quarterly"


# ---------------------------------------------------------------------------
# 3. CRUD: List recurring templates
# ---------------------------------------------------------------------------

def test_list_recurring_templates(setup_data):
    conn, company_id, _, _, lines = setup_data
    # Create 2 templates
    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Rent",
                 frequency="monthly", start_date="2026-03-01", lines=lines)
    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Insurance",
                 frequency="annual", start_date="2026-01-01", lines=lines)

    r = _call_action(list_recurring_templates, conn, company_id=company_id)
    assert r["total_count"] == 2
    assert len(r["templates"]) == 2


# ---------------------------------------------------------------------------
# 4. CRUD: Get + Delete recurring template
# ---------------------------------------------------------------------------

def test_get_and_delete_recurring_template(setup_data):
    conn, company_id, _, _, lines = setup_data
    r = _call_action(add_recurring_template, conn,
                     company_id=company_id, template_name="Rent",
                     frequency="monthly", start_date="2026-03-01", lines=lines)
    template_id = r["template_id"]

    # Get
    r2 = _call_action(get_recurring_template, conn, template_id=template_id)
    assert r2["name"] == "Rent"
    assert r2["frequency"] == "monthly"
    assert isinstance(r2["lines"], list)
    assert len(r2["lines"]) == 2

    # Delete
    r3 = _call_action(delete_recurring_template, conn, template_id=template_id)
    assert r3.get("status") == "ok"
    assert r3["deleted"] is True

    # Verify gone
    r4 = _call_action(list_recurring_templates, conn, company_id=company_id)
    assert r4["total_count"] == 0


# ---------------------------------------------------------------------------
# 5. Process generates correct JE (draft, since auto_submit=False)
# ---------------------------------------------------------------------------

def test_process_generates_draft_je(setup_data):
    conn, company_id, _, _, lines = setup_data
    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Rent",
                 frequency="monthly", start_date="2026-03-01", lines=lines)

    r = _call_action(process_recurring, conn,
                     company_id=company_id, as_of_date="2026-03-01")
    assert r["generated"] == 1
    je_id = r["results"][0]["journal_entry_id"]
    assert r["results"][0]["je_status"] == "draft"
    assert r["results"][0]["posting_date"] == "2026-03-01"

    # Verify JE exists in DB
    je = conn.execute("SELECT * FROM journal_entry WHERE id = ?", (je_id,)).fetchone()
    assert je is not None
    assert je["status"] == "draft"
    assert je["total_debit"] == "5000.00"
    assert je["total_credit"] == "5000.00"


# ---------------------------------------------------------------------------
# 6. Idempotent — re-run same day doesn't duplicate
# ---------------------------------------------------------------------------

def test_process_idempotent(setup_data):
    conn, company_id, _, _, lines = setup_data
    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Rent",
                 frequency="monthly", start_date="2026-03-01", lines=lines)

    # First run
    r1 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-03-01")
    assert r1["generated"] == 1

    # Second run same date — next_run_date is now 2026-04-01, so nothing due
    r2 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-03-01")
    assert r2["generated"] == 0

    # Verify only 1 JE created
    count = conn.execute(
        "SELECT COUNT(*) FROM journal_entry WHERE company_id = ?", (company_id,)
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 7. Monthly frequency date advancement
# ---------------------------------------------------------------------------

def test_monthly_frequency_advancement(setup_data):
    conn, company_id, _, _, lines = setup_data
    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Rent",
                 frequency="monthly", start_date="2026-01-31", lines=lines)

    # Process Jan
    r1 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-01-31")
    assert r1["generated"] == 1
    # Jan 31 + 1 month = Feb 28 (2026 is not a leap year)
    assert r1["results"][0]["next_run_date"] == "2026-02-28"

    # Process Feb
    r2 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-02-28")
    assert r2["generated"] == 1
    # Feb 28 + 1 month = Mar 28
    assert r2["results"][0]["next_run_date"] == "2026-03-28"


# ---------------------------------------------------------------------------
# 8. End date stops generation
# ---------------------------------------------------------------------------

def test_end_date_stops_generation(setup_data):
    conn, company_id, _, _, lines = setup_data
    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Short Lease",
                 frequency="monthly", start_date="2026-03-01",
                 end_date="2026-04-15", lines=lines)

    # Process March — should generate, next would be April
    r1 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-03-01")
    assert r1["generated"] == 1
    assert r1["results"][0]["template_status"] == "active"

    # Process April — should generate, but next (May 1) > end_date (Apr 15)
    r2 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-04-01")
    assert r2["generated"] == 1
    assert r2["results"][0]["template_status"] == "completed"

    # Process May — template is completed, nothing generated
    r3 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-05-01")
    assert r3["generated"] == 0


# ---------------------------------------------------------------------------
# 9. Multiple templates in one run
# ---------------------------------------------------------------------------

def test_multiple_templates_processed(setup_data):
    conn, company_id, _, _, lines = setup_data
    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Rent",
                 frequency="monthly", start_date="2026-03-01", lines=lines)
    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Insurance",
                 frequency="monthly", start_date="2026-03-01", lines=lines)

    r = _call_action(process_recurring, conn,
                     company_id=company_id, as_of_date="2026-03-01")
    assert r["generated"] == 2
    names = {res["template_name"] for res in r["results"]}
    assert names == {"Rent", "Insurance"}


# ---------------------------------------------------------------------------
# 10. Paused template is skipped
# ---------------------------------------------------------------------------

def test_paused_template_skipped(setup_data):
    conn, company_id, _, _, lines = setup_data
    r = _call_action(add_recurring_template, conn,
                     company_id=company_id, template_name="Rent",
                     frequency="monthly", start_date="2026-03-01", lines=lines)
    template_id = r["template_id"]

    # Pause it
    _call_action(update_recurring_template, conn,
                 template_id=template_id, template_status="paused")

    # Process — should generate nothing
    r2 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-03-01")
    assert r2["generated"] == 0

    # Re-activate
    _call_action(update_recurring_template, conn,
                 template_id=template_id, template_status="active")

    # Now it should work
    r3 = _call_action(process_recurring, conn,
                      company_id=company_id, as_of_date="2026-03-01")
    assert r3["generated"] == 1


# ---------------------------------------------------------------------------
# 11. Auto-submit + GL verification
# ---------------------------------------------------------------------------

def test_auto_submit_creates_gl(setup_data):
    conn, company_id, debit_acct, credit_acct, lines = setup_data
    # Need a cost center for P&L account validation
    import uuid
    cc_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)",
        (cc_id, "Main", company_id),
    )
    conn.commit()

    # Add cost_center_id to expense line
    lines_data = json.loads(lines)
    lines_data[0]["cost_center_id"] = cc_id
    lines_with_cc = json.dumps(lines_data)

    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Auto Rent",
                 frequency="monthly", start_date="2026-03-01",
                 auto_submit=True, lines=lines_with_cc)

    r = _call_action(process_recurring, conn,
                     company_id=company_id, as_of_date="2026-03-01")
    assert r["generated"] == 1
    assert r["results"][0]["je_status"] == "submitted"

    je_id = r["results"][0]["journal_entry_id"]

    # Verify GL entries exist
    gl_rows = conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (je_id,),
    ).fetchall()
    assert len(gl_rows) >= 2

    # Verify GL balance
    total_debit = sum(
        __import__("decimal").Decimal(row["debit"]) for row in gl_rows
    )
    total_credit = sum(
        __import__("decimal").Decimal(row["credit"]) for row in gl_rows
    )
    assert total_debit == total_credit


# ---------------------------------------------------------------------------
# 12. Invariant — GL remains balanced after multiple process runs
# ---------------------------------------------------------------------------

def test_invariant_gl_balanced_after_recurring(setup_data):
    conn, company_id, debit_acct, credit_acct, lines = setup_data
    import uuid
    cc_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)",
        (cc_id, "Main", company_id),
    )
    conn.commit()

    lines_data = json.loads(lines)
    lines_data[0]["cost_center_id"] = cc_id
    lines_with_cc = json.dumps(lines_data)

    _call_action(add_recurring_template, conn,
                 company_id=company_id, template_name="Monthly Rent",
                 frequency="monthly", start_date="2026-01-01",
                 auto_submit=True, lines=lines_with_cc)

    # Process 3 months
    for month_date in ["2026-01-01", "2026-02-01", "2026-03-01"]:
        _call_action(process_recurring, conn,
                     company_id=company_id, as_of_date=month_date)

    # Verify 3 JEs created
    je_count = conn.execute(
        "SELECT COUNT(*) FROM journal_entry WHERE company_id = ? AND status = 'submitted'",
        (company_id,),
    ).fetchone()[0]
    assert je_count == 3

    # GL invariant: SUM(debit) = SUM(credit) globally
    from decimal import Decimal
    gl_rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE is_cancelled = 0"
    ).fetchall()
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == total_credit
    assert total_debit == Decimal("15000.00")  # 3 × $5,000
