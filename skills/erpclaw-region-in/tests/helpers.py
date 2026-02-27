"""Shared test helpers for erpclaw-region-in tests.

Provides cross-skill fixture creation functions (customer, sales_invoice,
purchase_invoice) that use raw SQL.  The accepted pattern in this codebase
is that helpers.py can use raw SQL for cross-skill fixtures.
"""
import sqlite3
import uuid


# ---------------------------------------------------------------------------
# Cross-skill fixture helpers (raw SQL — these tables are owned by other skills)
# ---------------------------------------------------------------------------

def create_test_customer(db_path, name, tax_id=""):
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
                              shipping_state=""):
    """Insert a test sales invoice directly via SQL. Returns sales_invoice_id."""
    conn = sqlite3.connect(db_path)
    si_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice
           (id, name, company_id, customer_id, posting_date, net_total, total_tax,
            grand_total, docstatus, shipping_state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (si_id, name, company_id, customer_id, posting_date, net_total, total_tax,
         grand_total, docstatus, shipping_state),
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


# ---------------------------------------------------------------------------
# Composite seeding helpers
# ---------------------------------------------------------------------------

def seed_invoices(db_path, company_id):
    """Create test sales + purchase invoices for compliance report testing.

    Returns (si1_id, si2_id, pi1_id).
    """
    # B2B customer (with GSTIN)
    cust_b2b = create_test_customer(db_path, "ABC Corp", "29AABCU9603R1ZJ")

    # B2C customer (no GSTIN)
    cust_b2c = create_test_customer(db_path, "Walk-in Customer", "")

    # Sales invoice 1: B2B, Rs.50K + Rs.9K tax
    si1 = create_test_sales_invoice(
        db_path, company_id, cust_b2b, "INV-2026-00001", "2026-01-15",
        "50000", "9000", "59000", docstatus=1,
    )

    # Sales invoice 2: B2C, Rs.20K + Rs.3.6K tax
    si2 = create_test_sales_invoice(
        db_path, company_id, cust_b2c, "INV-2026-00002", "2026-01-20",
        "20000", "3600", "23600", docstatus=1, shipping_state="27",
    )

    # Purchase invoice: Rs.30K + Rs.5.4K tax
    pi1 = create_test_purchase_invoice(
        db_path, company_id, "PINV-2026-00001", "2026-01-10",
        "30000", "5400", "35400", docstatus=1,
    )

    return si1, si2, pi1
