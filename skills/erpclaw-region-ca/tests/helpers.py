"""Shared test helpers for erpclaw-region-ca tests.

Provides cross-skill fixture creation functions (customer, sales_invoice,
purchase_invoice, employee) that use raw SQL.  The accepted pattern in this
codebase is that helpers.py can use raw SQL for cross-skill fixtures.
"""
import sqlite3
import uuid


# ---------------------------------------------------------------------------
# Cross-skill fixture helpers (raw SQL — these tables are owned by other skills)
# ---------------------------------------------------------------------------

def create_test_customer(db_path, name="Acme Canada Inc", tax_id="123456789RT0001"):
    """Insert a test customer directly via SQL. Returns customer_id."""
    conn = sqlite3.connect(db_path)
    cust_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO customer (id, name, tax_id) VALUES (?, ?, ?)",
        (cust_id, name, tax_id),
    )
    conn.commit()
    conn.close()
    return cust_id


def create_test_sales_invoice(db_path, company_id, customer_id, name, posting_date,
                              net_total, total_tax, grand_total, docstatus=1,
                              province=""):
    """Insert a test sales invoice directly via SQL. Returns sales_invoice_id."""
    conn = sqlite3.connect(db_path)
    si_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice
           (id, name, company_id, customer_id, posting_date, net_total, total_tax,
            grand_total, docstatus, province)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (si_id, name, company_id, customer_id, posting_date, net_total, total_tax,
         grand_total, docstatus, province),
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


def create_test_employee(db_path, company_id, employee_name="John Smith",
                         first_name="John", last_name="Smith",
                         sin="046454286", province="ON",
                         date_of_joining="2025-01-15", status="active"):
    """Insert a test employee directly via SQL. Returns employee_id."""
    conn = sqlite3.connect(db_path)
    emp_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO employee
           (id, employee_name, first_name, last_name, sin, company_id, province,
            date_of_joining, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (emp_id, employee_name, first_name, last_name, sin, company_id, province,
         date_of_joining, status),
    )
    conn.commit()
    conn.close()
    return emp_id


# ---------------------------------------------------------------------------
# Composite seeding helpers
# ---------------------------------------------------------------------------

def seed_invoices(db_path, company_id):
    """Create test sales + purchase invoices for compliance report testing.

    Returns (si1_id, si2_id, pi1_id).
    """
    cust_id = create_test_customer(db_path, "Acme Canada Inc", "123456789RT0001")

    # Sales invoice 1: $50K + $6.5K HST (ON 13%)
    si1 = create_test_sales_invoice(
        db_path, company_id, cust_id, "INV-2026-00001", "2026-01-15",
        "50000", "6500", "56500", docstatus=1, province="ON",
    )

    # Sales invoice 2: $20K + $2.6K HST
    si2 = create_test_sales_invoice(
        db_path, company_id, cust_id, "INV-2026-00002", "2026-01-20",
        "20000", "2600", "22600", docstatus=1, province="ON",
    )

    # Purchase invoice: $30K + $3.9K HST
    pi1 = create_test_purchase_invoice(
        db_path, company_id, "PINV-2026-00001", "2026-01-10",
        "30000", "3900", "33900", docstatus=1,
    )

    return si1, si2, pi1
