"""S35-2: Large dataset stress tests.

Generates realistic large datasets and benchmarks query performance.
Targets: list queries < 1s, reports < 3s.
"""
import os
import sqlite3
import sys
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

from erpclaw_lib.db import _DecimalSum

_LOCAL_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../"))
_SERVER_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "erpclaw-setup", "scripts")
if os.path.exists(os.path.join(_LOCAL_ROOT, "init_db.py")):
    PROJECT_ROOT = _LOCAL_ROOT
elif os.path.exists(os.path.join(_SERVER_ROOT, "init_db.py")):
    PROJECT_ROOT = _SERVER_ROOT
else:
    PROJECT_ROOT = _LOCAL_ROOT


def _run_init_db(db_path):
    """Execute init_db.py to create all tables."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "init_db", os.path.join(PROJECT_ROOT, "init_db.py")
    )
    init_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_db)
    init_db.init_db(db_path)


@pytest.fixture(scope="module")
def large_db(tmp_path_factory):
    """Create a large dataset DB (module-scoped for performance).

    Disables FK enforcement during bulk insert for speed — this tests
    query performance, not referential integrity.
    """
    tmp_path = tmp_path_factory.mktemp("perftest")
    db_path = str(tmp_path / "perf.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # OFF for bulk insert speed
    conn.execute("PRAGMA busy_timeout=5000")
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)

    # --- Company ---
    company_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country, "
        "fiscal_year_start_month) VALUES (?, 'PerfTest Corp', 'PT', 'USD', 'US', 1)",
        (company_id,),
    )

    # --- Accounts ---
    accounts = {}
    for name, root_type, acct_num, acct_type in [
        ("Cash", "asset", "1000", "cash"),
        ("Bank", "asset", "1010", "bank"),
        ("AR", "asset", "1200", "receivable"),
        ("Inventory", "asset", "1300", "stock"),
        ("AP", "liability", "2000", "payable"),
        ("Revenue", "income", "4000", "revenue"),
        ("COGS", "expense", "5000", "cost_of_goods_sold"),
        ("OpEx", "expense", "6000", "expense"),
        ("Equity", "equity", "3000", "equity"),
    ]:
        aid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO account (id, name, company_id, root_type, "
            "account_number, account_type, is_group, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 'USD')",
            (aid, name, company_id, root_type, acct_num, acct_type),
        )
        accounts[name] = aid

    # --- Cost center + FY ---
    cc_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) "
        "VALUES (?, 'Main', ?, 0)", (cc_id, company_id),
    )
    fy_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id) "
        "VALUES (?, 'FY2026', '2026-01-01', '2026-12-31', ?)",
        (fy_id, company_id),
    )

    # --- 500 customers ---
    customer_ids = []
    cust_batch = []
    for i in range(500):
        cid = str(uuid.uuid4())
        cust_batch.append((cid, f"Customer-{i:04d}", company_id, "company", "USD"))
        customer_ids.append(cid)
    conn.executemany(
        "INSERT INTO customer (id, name, company_id, customer_type, "
        "default_currency) VALUES (?,?,?,?,?)", cust_batch,
    )

    # --- 200 suppliers ---
    supplier_ids = []
    sup_batch = []
    for i in range(200):
        sid = str(uuid.uuid4())
        sup_batch.append((sid, f"Supplier-{i:04d}", company_id, "company", "USD"))
        supplier_ids.append(sid)
    conn.executemany(
        "INSERT INTO supplier (id, name, company_id, supplier_type, "
        "default_currency) VALUES (?,?,?,?,?)", sup_batch,
    )

    # --- 100 items ---
    item_ids = []
    item_batch = []
    for i in range(100):
        iid = str(uuid.uuid4())
        item_batch.append((iid, f"ITEM-{i:04d}", f"Product {i}", "Nos", "fifo"))
        item_ids.append(iid)
    conn.executemany(
        "INSERT INTO item (id, item_code, item_name, stock_uom, "
        "valuation_method) VALUES (?,?,?,?,?)", item_batch,
    )

    # --- 10,000 GL entries (5000 balanced pairs) ---
    # gl_entry has NO company_id — company is via account_id -> account.company_id
    # gl_entry uses voucher_id (not voucher_no), fiscal_year (not fiscal_year_id)
    base_date = date(2026, 1, 1)
    gl_batch = []
    for i in range(5000):
        d = base_date + timedelta(days=i % 365)
        posting_date = d.isoformat()
        voucher_id = str(uuid.uuid4())
        amount = str(Decimal(str(100 + (i % 1000))))

        gle_id1 = str(uuid.uuid4())
        gle_id2 = str(uuid.uuid4())
        acct_from = accounts["Cash"] if i % 2 == 0 else accounts["Bank"]
        acct_to = accounts["Revenue"] if i % 3 != 0 else accounts["OpEx"]

        gl_batch.append((gle_id1, posting_date,
                         acct_from, amount, "0",
                         "journal_entry", voucher_id,
                         cc_id, "USD", "1", amount, "0", "FY2026"))
        gl_batch.append((gle_id2, posting_date,
                         acct_to, "0", amount,
                         "journal_entry", voucher_id,
                         cc_id, "USD", "1", "0", amount, "FY2026"))

    conn.executemany(
        "INSERT INTO gl_entry (id, posting_date, account_id, "
        "debit, credit, voucher_type, voucher_id, "
        "cost_center_id, currency, exchange_rate, debit_base, credit_base, "
        "fiscal_year) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", gl_batch,
    )

    # --- 1000 sales invoices ---
    inv_batch = []
    for i in range(1000):
        inv_id = str(uuid.uuid4())
        cust = customer_ids[i % len(customer_ids)]
        d = base_date + timedelta(days=i % 365)
        amount = str(Decimal(str(500 + (i % 5000))))
        inv_batch.append((inv_id, company_id, cust, d.isoformat(),
                          (d + timedelta(days=30)).isoformat(),
                          "USD", "1", amount, amount, amount, "submitted"))
    conn.executemany(
        "INSERT INTO sales_invoice (id, company_id, customer_id, "
        "posting_date, due_date, currency, exchange_rate, "
        "total_amount, grand_total, outstanding_amount, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", inv_batch,
    )

    # --- 500 purchase invoices ---
    pinv_batch = []
    for i in range(500):
        pinv_id = str(uuid.uuid4())
        sup = supplier_ids[i % len(supplier_ids)]
        d = base_date + timedelta(days=i % 365)
        amount = str(Decimal(str(300 + (i % 3000))))
        pinv_batch.append((pinv_id, company_id, sup, d.isoformat(),
                           (d + timedelta(days=30)).isoformat(),
                           "USD", "1", amount, amount, amount, "submitted"))
    conn.executemany(
        "INSERT INTO purchase_invoice (id, company_id, supplier_id, "
        "posting_date, due_date, currency, exchange_rate, "
        "total_amount, grand_total, outstanding_amount, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", pinv_batch,
    )

    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    # Collect all account IDs for GL queries (gl_entry has no company_id)
    account_ids = list(accounts.values())
    yield conn, company_id, accounts, account_ids, db_path
    conn.close()


# ===========================================================================
# Benchmark helper
# ===========================================================================

def _time_query(conn, sql, params=None):
    """Time a query and return (elapsed_seconds, result_count)."""
    start = time.perf_counter()
    rows = conn.execute(sql, params or []).fetchall()
    elapsed = time.perf_counter() - start
    return elapsed, len(rows)


# ===========================================================================
# Tests
# ===========================================================================

def _gl_acct_placeholders(account_ids):
    """Build (placeholders, params) for GL queries filtering by account_id."""
    ph = ",".join("?" for _ in account_ids)
    return ph, account_ids


class TestListQueryPerformance:
    """List queries should complete in < 1 second."""

    def test_gl_entry_list(self, large_db):
        conn, company_id, _, account_ids, _ = large_db
        ph, params = _gl_acct_placeholders(account_ids)
        elapsed, count = _time_query(
            conn,
            f"SELECT * FROM gl_entry WHERE account_id IN ({ph}) "
            "ORDER BY posting_date DESC LIMIT 50",
            params,
        )
        assert elapsed < 1.0, f"GL list took {elapsed:.2f}s"
        assert count == 50

    def test_customer_list(self, large_db):
        conn, company_id, _, _, _ = large_db
        elapsed, count = _time_query(
            conn,
            "SELECT * FROM customer WHERE company_id = ? ORDER BY name LIMIT 50",
            [company_id],
        )
        assert elapsed < 1.0, f"Customer list took {elapsed:.2f}s"
        assert count == 50

    def test_sales_invoice_list(self, large_db):
        conn, company_id, _, _, _ = large_db
        elapsed, count = _time_query(
            conn,
            "SELECT * FROM sales_invoice WHERE company_id = ? AND status = 'submitted' "
            "ORDER BY posting_date DESC LIMIT 50",
            [company_id],
        )
        assert elapsed < 1.0, f"Invoice list took {elapsed:.2f}s"
        assert count == 50

    def test_supplier_list(self, large_db):
        conn, company_id, _, _, _ = large_db
        elapsed, count = _time_query(
            conn,
            "SELECT * FROM supplier WHERE company_id = ? ORDER BY name LIMIT 50",
            [company_id],
        )
        assert elapsed < 1.0, f"Supplier list took {elapsed:.2f}s"
        assert count == 50


class TestReportQueryPerformance:
    """Report queries should complete in < 3 seconds."""

    def test_trial_balance_aggregate(self, large_db):
        """Trial balance — SUM all GL by account."""
        conn, company_id, _, account_ids, _ = large_db
        ph, params = _gl_acct_placeholders(account_ids)
        elapsed, count = _time_query(
            conn,
            f"SELECT account_id, "
            "SUM(CAST(debit AS REAL)) as total_debit, "
            "SUM(CAST(credit AS REAL)) as total_credit "
            f"FROM gl_entry WHERE account_id IN ({ph}) "
            "GROUP BY account_id",
            params,
        )
        assert elapsed < 3.0, f"TB aggregate took {elapsed:.2f}s"
        assert count > 0

    def test_gl_date_range(self, large_db):
        """GL entries for a date range (month)."""
        conn, company_id, _, account_ids, _ = large_db
        ph, params = _gl_acct_placeholders(account_ids)
        elapsed, count = _time_query(
            conn,
            f"SELECT * FROM gl_entry WHERE account_id IN ({ph}) "
            "AND posting_date BETWEEN '2026-01-01' AND '2026-01-31' "
            "ORDER BY posting_date",
            params,
        )
        assert elapsed < 3.0, f"GL date range took {elapsed:.2f}s"

    def test_aging_report(self, large_db):
        """Outstanding invoices for aging report."""
        conn, company_id, _, _, _ = large_db
        elapsed, count = _time_query(
            conn,
            "SELECT customer_id, outstanding_amount, due_date "
            "FROM sales_invoice WHERE company_id = ? "
            "AND outstanding_amount != '0' AND status = 'submitted' "
            "ORDER BY due_date",
            [company_id],
        )
        assert elapsed < 3.0, f"Aging report took {elapsed:.2f}s"


class TestDataIntegrity:
    """Data integrity checks on the large dataset."""

    def test_gl_balanced(self, large_db):
        """10K GL entries should be balanced (debits = credits)."""
        conn, company_id, _, account_ids, _ = large_db
        ph, params = _gl_acct_placeholders(account_ids)
        result = conn.execute(
            f"SELECT SUM(CAST(debit AS REAL)) as d, "
            "SUM(CAST(credit AS REAL)) as c "
            f"FROM gl_entry WHERE account_id IN ({ph})",
            params,
        ).fetchone()
        assert abs(result["d"] - result["c"]) < 0.01, \
            f"GL unbalanced: {result['d']} != {result['c']}"

    def test_row_counts(self, large_db):
        """Verify expected row counts."""
        conn, company_id, _, account_ids, _ = large_db
        ph, params = _gl_acct_placeholders(account_ids)
        gl_count = conn.execute(
            f"SELECT COUNT(*) as cnt FROM gl_entry WHERE account_id IN ({ph})",
            params,
        ).fetchone()["cnt"]
        assert gl_count == 10000  # 5000 pairs

        cust_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM customer WHERE company_id = ?",
            [company_id],
        ).fetchone()["cnt"]
        assert cust_count == 500

        inv_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM sales_invoice WHERE company_id = ?",
            [company_id],
        ).fetchone()["cnt"]
        assert inv_count == 1000


class TestCompositeIndexes:
    """S35-3: Verify composite indexes exist and improve query plans."""

    EXPECTED_COMPOSITES = [
        "idx_account_co_root_type",
        "idx_journal_entry_co_status_date",
        "idx_payment_entry_co_status_date",
        "idx_ple_party_date",
        "idx_customer_co_status",
        "idx_sales_invoice_co_status_date",
        "idx_supplier_co_status",
        "idx_purchase_invoice_co_status_date",
        "idx_sle_warehouse_date",
        "idx_employee_co_status",
    ]

    def test_composite_indexes_exist(self, large_db):
        """All 10 composite indexes must be present in the DB."""
        conn, _, _, _, _ = large_db
        for idx_name in self.EXPECTED_COMPOSITES:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (idx_name,),
            ).fetchone()
            assert row is not None, f"Missing composite index: {idx_name}"

    def test_composite_indexes_used_in_query_plans(self, large_db):
        """Key queries should use composite indexes (SEARCH, not SCAN)."""
        conn, company_id, _, _, _ = large_db
        test_queries = [
            (
                "SELECT * FROM sales_invoice WHERE company_id = ? "
                "AND status = 'submitted' ORDER BY posting_date DESC LIMIT 10",
                [company_id],
                "idx_sales_invoice_co_status_date",
            ),
            (
                "SELECT * FROM purchase_invoice WHERE company_id = ? "
                "AND status = 'submitted' ORDER BY posting_date DESC LIMIT 10",
                [company_id],
                "idx_purchase_invoice_co_status_date",
            ),
            (
                "SELECT * FROM customer WHERE company_id = ? AND status = 'active'",
                [company_id],
                "idx_customer_co_status",
            ),
            (
                "SELECT * FROM supplier WHERE company_id = ? AND status = 'active'",
                [company_id],
                "idx_supplier_co_status",
            ),
        ]
        for sql, params, expected_idx in test_queries:
            plan_rows = conn.execute(
                f"EXPLAIN QUERY PLAN {sql}", params
            ).fetchall()
            plan_text = " ".join(str(r["detail"]) for r in plan_rows)
            # Composite index should appear, or at minimum no SCAN
            assert "SCAN" not in plan_text or expected_idx in plan_text, \
                f"Query not using composite index {expected_idx}. Plan: {plan_text}"
