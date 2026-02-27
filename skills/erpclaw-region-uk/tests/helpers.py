"""Shared test helpers for erpclaw-region-uk tests.

Provides cross-skill fixture creation functions (sales_invoice, purchase_invoice,
employee, salary_slip) that use raw SQL.  The accepted pattern in this codebase
is that helpers.py can use raw SQL for cross-skill fixtures.
"""
import sqlite3
import uuid


# ---------------------------------------------------------------------------
# Cross-skill fixture helpers (raw SQL — these tables are owned by other skills)
# ---------------------------------------------------------------------------

def create_test_sales_invoice(db_path, company_id, name, posting_date,
                              net_total, total_tax, grand_total, docstatus=1):
    """Insert a test sales invoice directly via SQL. Returns sales_invoice_id."""
    conn = sqlite3.connect(db_path)
    si_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice
           (id, name, company_id, posting_date, net_total, total_tax, grand_total,
            docstatus)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (si_id, name, company_id, posting_date, net_total, total_tax, grand_total,
         docstatus),
    )
    conn.commit()
    conn.close()
    return si_id


def create_test_purchase_invoice(db_path, company_id, name, posting_date,
                                 net_total, total_tax, grand_total, docstatus=1):
    """Insert a test purchase invoice directly via SQL. Returns purchase_invoice_id."""
    conn = sqlite3.connect(db_path)
    pi_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO purchase_invoice
           (id, name, company_id, posting_date, net_total, total_tax, grand_total,
            docstatus)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (pi_id, name, company_id, posting_date, net_total, total_tax, grand_total,
         docstatus),
    )
    conn.commit()
    conn.close()
    return pi_id


def create_test_employee(db_path, company_id, employee_name, first_name, last_name,
                         nino="", status="active", date_of_leaving=""):
    """Insert a test employee directly via SQL. Returns employee_id."""
    conn = sqlite3.connect(db_path)
    emp_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO employee
           (id, employee_name, first_name, last_name, nino, company_id,
            date_of_leaving, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (emp_id, employee_name, first_name, last_name, nino, company_id,
         date_of_leaving, status),
    )
    conn.commit()
    conn.close()
    return emp_id


def create_test_salary_slip(db_path, employee_id, company_id, posting_date,
                            gross_pay, total_deduction, net_pay,
                            payroll_period="", docstatus=1):
    """Insert a test salary slip directly via SQL. Returns salary_slip_id."""
    conn = sqlite3.connect(db_path)
    # Create a payroll_run first (required FK)
    run_id = str(uuid.uuid4())
    period_start = posting_date[:8] + "01"  # First of the month
    period_end = posting_date
    status = "submitted" if docstatus == 1 else "draft"
    conn.execute(
        """INSERT OR IGNORE INTO payroll_run
           (id, period_start, period_end, status, company_id)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, period_start, period_end, status, company_id),
    )
    slip_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO salary_slip
           (id, payroll_run_id, employee_id, company_id, period_start, period_end,
            gross_pay, total_deductions, net_pay, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (slip_id, run_id, employee_id, company_id, period_start, period_end,
         gross_pay, total_deduction, net_pay, status),
    )
    conn.commit()
    conn.close()
    return slip_id
