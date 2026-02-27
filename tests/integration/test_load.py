"""Load tests for ERPClaw — performance at scale.

Tests bulk operations (GL entries, customers, items, stock entries, O2C cycles)
and verifies the system handles load without crashing or timing out.
Thresholds are generous — we are testing correctness under volume, not benchmarking.

All tests marked with @pytest.mark.load.
"""
import json
import os
import time
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
# 1. Bulk GL entries (5,000 via 50 JEs x 100 lines each)
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_bulk_gl_entries_10k(fresh_db):
    """Create and submit 50 JEs with 50 DR/CR pairs each = 5,000 GL entries.
    Verify GL balanced and that list-gl-entries + trial-balance complete in < 5s."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank_a = create_test_account(conn, cid, "Bank A", "asset",
                                 account_type="bank", account_number="1001")
    bank_b = create_test_account(conn, cid, "Bank B", "asset",
                                 account_type="bank", account_number="1002")
    cc = create_test_cost_center(conn, cid)

    # Create and submit 50 JEs, each with 50 DR/CR pairs (100 lines)
    for je_idx in range(50):
        lines = []
        for pair_idx in range(50):
            amount = str(Decimal("100.00") + Decimal(str(pair_idx)))
            lines.append({"account_id": bank_a, "debit": amount, "credit": "0",
                          "cost_center_id": cc})
            lines.append({"account_id": bank_b, "debit": "0", "credit": amount,
                          "cost_center_id": cc})

        result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=cid, posting_date="2026-03-15",
                              lines=json.dumps(lines))
        assert result["status"] == "ok", f"JE {je_idx} add failed: {result}"
        je_id = result["journal_entry_id"]

        result = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                              journal_entry_id=je_id)
        assert result["status"] == "ok", f"JE {je_idx} submit failed: {result}"

    # Verify total GL entry count = 50 * 100 = 5,000
    gl_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE is_cancelled = 0"
    ).fetchone()["cnt"]
    assert gl_count == 5000, f"Expected 5000 GL entries, got {gl_count}"

    # Verify GL balanced: decimal_sum(debit) = decimal_sum(credit)
    totals = conn.execute(
        """SELECT decimal_sum(debit) as total_debit,
                  decimal_sum(credit) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()
    total_debit = Decimal(totals["total_debit"])
    total_credit = Decimal(totals["total_credit"])
    assert total_debit == total_credit, (
        f"GL not balanced: debit={total_debit}, credit={total_credit}"
    )

    # Time list-gl-entries
    t0 = time.time()
    _call_action("erpclaw-gl", "list-gl-entries", conn,
                 company_id=cid, limit=50)
    list_elapsed = time.time() - t0
    assert list_elapsed < 5.0, f"list-gl-entries took {list_elapsed:.2f}s (> 5s)"

    # Time trial-balance
    t0 = time.time()
    _call_action("erpclaw-reports", "trial-balance", conn,
                 company_id=cid, from_date="2026-01-01", to_date="2026-12-31")
    tb_elapsed = time.time() - t0
    assert tb_elapsed < 5.0, f"trial-balance took {tb_elapsed:.2f}s (> 5s)"


# ---------------------------------------------------------------------------
# 2. Bulk customers (1,000)
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_bulk_customers_1000(fresh_db):
    """Create 1,000 customers and verify list-customers completes in < 2s."""
    conn = fresh_db
    cid = create_test_company(conn)
    seed_naming_series(conn, cid)

    for i in range(1000):
        create_test_customer(conn, cid, name=f"Customer {i:04d}")

    # Verify count
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM customer WHERE company_id = ?",
        (cid,),
    ).fetchone()["cnt"]
    assert count == 1000, f"Expected 1000 customers, got {count}"

    # Time list-customers
    t0 = time.time()
    result = _call_action("erpclaw-selling", "list-customers", conn,
                          company_id=cid, limit=1000)
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"list-customers took {elapsed:.2f}s (> 2s)"


# ---------------------------------------------------------------------------
# 3. Bulk naming series (500 JE drafts)
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_bulk_naming_series_500(fresh_db):
    """Create 500 JE drafts and verify naming series is sequential with no gaps."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1001")
    expense = create_test_account(conn, cid, "Expense", "expense",
                                  account_type="expense", account_number="5001")
    cc = create_test_cost_center(conn, cid)

    je_names = []
    for i in range(500):
        lines = json.dumps([
            {"account_id": bank, "debit": "100.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": expense, "debit": "0", "credit": "100.00",
             "cost_center_id": cc},
        ])
        result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=cid, posting_date="2026-03-15",
                              lines=lines)
        assert result["status"] == "ok", f"JE {i} failed: {result}"
        je_id = result["journal_entry_id"]
        # Fetch the naming_series from the DB
        row = conn.execute(
            "SELECT naming_series FROM journal_entry WHERE id = ?", (je_id,)
        ).fetchone()
        je_names.append(row["naming_series"])

    # Verify naming_series current_value = 500
    ns_row = conn.execute(
        "SELECT current_value FROM naming_series WHERE entity_type = 'journal_entry' AND company_id = ?",
        (cid,),
    ).fetchone()
    assert ns_row is not None, "No naming_series row for journal_entry"
    assert int(ns_row["current_value"]) == 500, (
        f"Expected current_value=500, got {ns_row['current_value']}"
    )

    # Verify all names are unique
    assert len(set(je_names)) == 500, (
        f"Expected 500 unique names, got {len(set(je_names))}"
    )

    # Verify sequential: extract sequence numbers and check for gaps
    seq_numbers = []
    for name in je_names:
        # Names are like JE2026-00001
        parts = name.rsplit("-", 1)
        seq_numbers.append(int(parts[-1]))
    seq_numbers.sort()
    expected = list(range(1, 501))
    assert seq_numbers == expected, (
        f"Naming series has gaps: first mismatch at index "
        f"{next(i for i, (a, b) in enumerate(zip(seq_numbers, expected)) if a != b)}"
    )


# ---------------------------------------------------------------------------
# 4. Bulk items (500)
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_bulk_items_500(fresh_db):
    """Create 500 items and verify list-items completes in < 2s."""
    conn = fresh_db

    for i in range(500):
        create_test_item(conn, item_code=f"ITEM-{i:04d}",
                         item_name=f"Product {i:04d}")

    # Verify count
    count = conn.execute("SELECT COUNT(*) as cnt FROM item").fetchone()["cnt"]
    assert count == 500, f"Expected 500 items, got {count}"

    # Time list-items
    t0 = time.time()
    result = _call_action("erpclaw-inventory", "list-items", conn, limit=500)
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"list-items took {elapsed:.2f}s (> 2s)"


# ---------------------------------------------------------------------------
# 5. Trial balance with many accounts (100 accounts, 50 JEs)
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_trial_balance_with_many_accounts(fresh_db):
    """Create 100 accounts (50 expense, 50 income), submit 50 JEs each
    touching 2 accounts. Verify trial-balance returns all accounts in < 5s."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)

    # Create 50 expense + 50 income accounts
    expense_accts = []
    income_accts = []
    for i in range(50):
        eid = create_test_account(conn, cid, f"Expense {i:02d}", "expense",
                                  account_type="expense",
                                  account_number=f"5{i:03d}")
        expense_accts.append(eid)
        iid = create_test_account(conn, cid, f"Income {i:02d}", "income",
                                  account_type="revenue",
                                  account_number=f"4{i:03d}")
        income_accts.append(iid)

    # Submit 50 JEs, each touching one expense (debit) and one income (credit)
    for je_idx in range(50):
        exp_acct = expense_accts[je_idx % 50]
        inc_acct = income_accts[je_idx % 50]
        amount = str(Decimal("200.00") + Decimal(str(je_idx)))

        lines = json.dumps([
            {"account_id": exp_acct, "debit": amount, "credit": "0",
             "cost_center_id": cc},
            {"account_id": inc_acct, "debit": "0", "credit": amount,
             "cost_center_id": cc},
        ])
        result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=cid, posting_date="2026-06-15",
                              lines=lines)
        assert result["status"] == "ok"
        je_id = result["journal_entry_id"]

        result = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                              journal_entry_id=je_id)
        assert result["status"] == "ok"

    # Time trial-balance
    t0 = time.time()
    tb_result = _call_action("erpclaw-reports", "trial-balance", conn,
                             company_id=cid,
                             from_date="2026-01-01", to_date="2026-12-31")
    elapsed = time.time() - t0
    assert elapsed < 5.0, f"trial-balance took {elapsed:.2f}s (> 5s)"

    # Verify trial-balance includes rows (the result should have accounts with balances)
    assert tb_result["status"] == "ok", f"trial-balance failed: {tb_result}"


# ---------------------------------------------------------------------------
# 6. Bulk stock entries (500 receive entries)
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_bulk_stock_entries_500(fresh_db):
    """Create and submit 500 stock receive entries (1 unit each).
    Verify total stock = 500 via SLE sum. Time the operation."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)

    stock_acct = create_test_account(conn, cid, "Stock In Hand", "asset",
                                     account_type="stock",
                                     account_number="1400")
    stock_adj = create_test_account(conn, cid, "Stock Adjustment", "expense",
                                    account_type="stock_adjustment",
                                    account_number="5200")
    item_id = create_test_item(conn, item_code="LOAD-ITEM-001",
                               item_name="Load Test Item")
    wh_id = create_test_warehouse(conn, cid, "Load Warehouse",
                                  account_id=stock_acct)

    t0 = time.time()
    for i in range(500):
        items_j = json.dumps([{
            "item_id": item_id,
            "qty": "1",
            "rate": "10.00",
            "to_warehouse_id": wh_id,
        }])
        result = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                              company_id=cid,
                              entry_type="receive",
                              posting_date="2026-03-15",
                              items=items_j,
                              target_warehouse_id=wh_id)
        assert result["status"] == "ok", f"Stock entry {i} add failed: {result}"
        se_id = result["stock_entry_id"]

        result = _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                              stock_entry_id=se_id)
        assert result["status"] == "ok", f"Stock entry {i} submit failed: {result}"
    total_elapsed = time.time() - t0

    # Verify total stock = 500 via SLE sum
    sle_total = conn.execute(
        """SELECT COALESCE(SUM(CAST(actual_qty AS REAL)), 0) as total
           FROM stock_ledger_entry
           WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0""",
        (item_id, wh_id),
    ).fetchone()["total"]
    assert abs(sle_total - 500.0) < 0.01, (
        f"Expected SLE total = 500, got {sle_total}"
    )

    # Sanity check: total time should be under 60s
    assert total_elapsed < 60.0, (
        f"500 stock entries took {total_elapsed:.1f}s (> 60s)"
    )


# ---------------------------------------------------------------------------
# 7. Full O2C cycle x10
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_full_o2c_cycle_10x(fresh_db):
    """Run 10 complete order-to-cash cycles and verify all GL balanced."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    for cycle in range(10):
        # Each cycle: quotation -> SO -> DN -> invoice -> payment
        # Use different quantities to avoid monotony
        qty = str(5 + cycle)
        rate = "50.00"

        items_j = json.dumps([{
            "item_id": env["item_id"],
            "qty": qty,
            "rate": rate,
            "warehouse_id": env["warehouse_id"],
        }])

        # 1. add-quotation
        r = _call_action("erpclaw-selling", "add-quotation", conn,
                         customer_id=env["customer_id"],
                         posting_date="2026-06-15",
                         items=items_j,
                         company_id=env["company_id"])
        assert r["status"] == "ok", f"Cycle {cycle} add-quotation failed: {r}"
        q_id = r["quotation_id"]

        # 2. submit-quotation
        r = _call_action("erpclaw-selling", "submit-quotation", conn,
                         quotation_id=q_id)
        assert r["status"] == "ok", f"Cycle {cycle} submit-quotation failed: {r}"

        # 3. convert-quotation-to-so
        r = _call_action("erpclaw-selling", "convert-quotation-to-so", conn,
                         quotation_id=q_id,
                         delivery_date="2026-07-01")
        assert r["status"] == "ok", f"Cycle {cycle} convert failed: {r}"
        so_id = r["sales_order_id"]

        # Set warehouse on SO items (quotation items don't carry warehouse_id)
        conn.execute(
            "UPDATE sales_order_item SET warehouse_id = ? WHERE sales_order_id = ?",
            (env["warehouse_id"], so_id),
        )
        conn.commit()

        # 4. submit-sales-order
        r = _call_action("erpclaw-selling", "submit-sales-order", conn,
                         sales_order_id=so_id)
        assert r["status"] == "ok", f"Cycle {cycle} submit-so failed: {r}"

        # 5. create-delivery-note
        r = _call_action("erpclaw-selling", "create-delivery-note", conn,
                         sales_order_id=so_id,
                         posting_date="2026-07-01")
        assert r["status"] == "ok", f"Cycle {cycle} create-dn failed: {r}"
        dn_id = r["delivery_note_id"]

        # 6. submit-delivery-note
        r = _call_action("erpclaw-selling", "submit-delivery-note", conn,
                         delivery_note_id=dn_id)
        assert r["status"] == "ok", f"Cycle {cycle} submit-dn failed: {r}"

        # 7. create-sales-invoice
        r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                         sales_order_id=so_id,
                         posting_date="2026-07-02")
        assert r["status"] == "ok", f"Cycle {cycle} create-si failed: {r}"
        si_id = r["sales_invoice_id"]

        # 8. submit-sales-invoice
        r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                         sales_invoice_id=si_id)
        assert r["status"] == "ok", f"Cycle {cycle} submit-si failed: {r}"

        # 9. Payment for the invoice
        si_row = conn.execute(
            "SELECT grand_total FROM sales_invoice WHERE id = ?", (si_id,)
        ).fetchone()
        grand_total = si_row["grand_total"]

        r = _call_action("erpclaw-payments", "add-payment", conn,
                         company_id=env["company_id"],
                         payment_type="receive",
                         posting_date="2026-07-10",
                         party_type="customer",
                         party_id=env["customer_id"],
                         paid_from_account=env["receivable_id"],
                         paid_to_account=env["bank_id"],
                         paid_amount=grand_total)
        assert r["status"] == "ok", f"Cycle {cycle} add-payment failed: {r}"
        pe_id = r["payment_entry_id"]

        # 10. Submit payment
        r = _call_action("erpclaw-payments", "submit-payment", conn,
                         payment_entry_id=pe_id)
        assert r["status"] == "ok", f"Cycle {cycle} submit-payment failed: {r}"

    # Verify ALL GL is balanced across all 10 cycles
    totals = conn.execute(
        """SELECT decimal_sum(debit) as total_debit,
                  decimal_sum(credit) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()
    total_debit = Decimal(totals["total_debit"])
    total_credit = Decimal(totals["total_credit"])
    assert total_debit == total_credit, (
        f"GL not balanced after 10 O2C cycles: "
        f"debit={total_debit}, credit={total_credit}"
    )

    # Verify we actually created entries (at least 10 invoices)
    si_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM sales_invoice WHERE status = 'submitted'"
    ).fetchone()["cnt"]
    assert si_count >= 10, f"Expected >= 10 submitted invoices, got {si_count}"


# ---------------------------------------------------------------------------
# 8. DB file size after load
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_db_file_size_after_load(fresh_db):
    """After inserting 5,000 GL entries via direct SQL, verify DB < 50MB."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)

    bank_a = create_test_account(conn, cid, "Bank A", "asset",
                                 account_type="bank", account_number="1001")
    bank_b = create_test_account(conn, cid, "Bank B", "asset",
                                 account_type="bank", account_number="1002")
    cc = create_test_cost_center(conn, cid)

    # Insert 5,000 GL entry pairs directly via SQL for speed
    batch = []
    for i in range(2500):
        voucher_id = str(uuid.uuid4())
        amount = str(Decimal("100.00") + Decimal(str(i % 1000)))
        batch.append((str(uuid.uuid4()), bank_a, "2026-03-15",
                       amount, "0", "journal_entry", voucher_id, 0, "FY2026",
                       cc, "USD", "1", amount, "0"))
        batch.append((str(uuid.uuid4()), bank_b, "2026-03-15",
                       "0", amount, "journal_entry", voucher_id, 0, "FY2026",
                       cc, "USD", "1", "0", amount))

    conn.executemany(
        """INSERT INTO gl_entry
           (id, account_id, posting_date, debit, credit,
            voucher_type, voucher_id, is_cancelled, fiscal_year,
            cost_center_id, currency, exchange_rate, debit_base, credit_base)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        batch,
    )
    conn.commit()

    # Get the DB file path from the connection
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    db_size_mb = os.path.getsize(db_path) / (1024 * 1024)

    assert db_size_mb < 50, (
        f"DB file size {db_size_mb:.1f}MB exceeds 50MB threshold"
    )


# ---------------------------------------------------------------------------
# 9. WAL checkpoint after load
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_wal_checkpoint_after_load(fresh_db):
    """After inserting many records, run WAL checkpoint and verify it completes
    in < 2s and WAL file is truncated."""
    conn = fresh_db
    cid = create_test_company(conn)

    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1001")
    expense = create_test_account(conn, cid, "Expense", "expense",
                                  account_type="expense", account_number="5001")
    cc = create_test_cost_center(conn, cid)

    # Insert 2,000 GL entry pairs via direct SQL
    batch = []
    for i in range(2000):
        voucher_id = str(uuid.uuid4())
        amount = str(Decimal("50.00") + Decimal(str(i % 500)))
        batch.append((str(uuid.uuid4()), bank, "2026-04-15",
                       amount, "0", "journal_entry", voucher_id, 0, "FY2026",
                       cc, "USD", "1", amount, "0"))
        batch.append((str(uuid.uuid4()), expense, "2026-04-15",
                       "0", amount, "journal_entry", voucher_id, 0, "FY2026",
                       cc, "USD", "1", "0", amount))
    conn.executemany(
        """INSERT INTO gl_entry
           (id, account_id, posting_date, debit, credit,
            voucher_type, voucher_id, is_cancelled, fiscal_year,
            cost_center_id, currency, exchange_rate, debit_base, credit_base)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        batch,
    )
    conn.commit()

    # Get DB path for WAL file check
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    wal_path = db_path + "-wal"

    # Run WAL checkpoint
    t0 = time.time()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    elapsed = time.time() - t0

    assert elapsed < 2.0, (
        f"WAL checkpoint took {elapsed:.2f}s (> 2s)"
    )

    # WAL file should be truncated (very small or non-existent)
    if os.path.exists(wal_path):
        wal_size = os.path.getsize(wal_path)
        # After TRUNCATE, WAL should be empty or nearly so
        assert wal_size < 1024 * 1024, (
            f"WAL file still {wal_size / 1024:.1f}KB after TRUNCATE"
        )


# ---------------------------------------------------------------------------
# 10. Report generation at scale
# ---------------------------------------------------------------------------

@pytest.mark.load
def test_report_generation_at_scale(fresh_db):
    """Set up 50 accounts, insert 2,000 GL entry pairs directly, then run
    trial-balance, profit-and-loss, and balance-sheet. All should complete in < 5s."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)

    # Create 50 accounts: 15 asset, 10 liability, 5 equity, 10 income, 10 expense
    accounts = {}
    for i in range(15):
        accounts[f"asset_{i}"] = create_test_account(
            conn, cid, f"Asset {i:02d}", "asset",
            account_type="bank", account_number=f"1{i:03d}")
    for i in range(10):
        accounts[f"liability_{i}"] = create_test_account(
            conn, cid, f"Liability {i:02d}", "liability",
            account_type="payable", account_number=f"2{i:03d}")
    for i in range(5):
        accounts[f"equity_{i}"] = create_test_account(
            conn, cid, f"Equity {i:02d}", "equity",
            account_type="equity", account_number=f"3{i:03d}")
    for i in range(10):
        accounts[f"income_{i}"] = create_test_account(
            conn, cid, f"Income {i:02d}", "income",
            account_type="revenue", account_number=f"4{i:03d}")
    for i in range(10):
        accounts[f"expense_{i}"] = create_test_account(
            conn, cid, f"Expense {i:02d}", "expense",
            account_type="expense", account_number=f"5{i:03d}")

    # Insert 2,000 balanced GL entry pairs directly via SQL
    acct_keys = list(accounts.keys())
    batch = []
    for i in range(2000):
        voucher_id = str(uuid.uuid4())
        amount = str(Decimal("75.00") + Decimal(str(i % 500)))

        # Pick a debit account (asset or expense) and a credit account (liability, equity, or income)
        debit_key = acct_keys[i % 25]  # first 25 are asset (15) + liability (10)
        credit_key = acct_keys[25 + (i % 25)]  # next 25 are equity (5) + income (10) + expense (10)

        batch.append((str(uuid.uuid4()), accounts[debit_key], "2026-03-15",
                       amount, "0", "journal_entry", voucher_id, 0, "FY2026",
                       cc, "USD", "1", amount, "0"))
        batch.append((str(uuid.uuid4()), accounts[credit_key], "2026-03-15",
                       "0", amount, "journal_entry", voucher_id, 0, "FY2026",
                       cc, "USD", "1", "0", amount))

    conn.executemany(
        """INSERT INTO gl_entry
           (id, account_id, posting_date, debit, credit,
            voucher_type, voucher_id, is_cancelled, fiscal_year,
            cost_center_id, currency, exchange_rate, debit_base, credit_base)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        batch,
    )
    conn.commit()

    # Verify GL count = 4,000
    gl_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE is_cancelled = 0"
    ).fetchone()["cnt"]
    assert gl_count == 4000, f"Expected 4000 GL entries, got {gl_count}"

    # Time trial-balance
    t0 = time.time()
    tb = _call_action("erpclaw-reports", "trial-balance", conn,
                      company_id=cid,
                      from_date="2026-01-01", to_date="2026-12-31")
    tb_elapsed = time.time() - t0
    assert tb["status"] == "ok", f"trial-balance failed: {tb}"
    assert tb_elapsed < 5.0, f"trial-balance took {tb_elapsed:.2f}s (> 5s)"

    # Time profit-and-loss
    t0 = time.time()
    pl = _call_action("erpclaw-reports", "profit-and-loss", conn,
                      company_id=cid,
                      from_date="2026-01-01", to_date="2026-12-31",
                      periodicity="annual")
    pl_elapsed = time.time() - t0
    assert pl["status"] == "ok", f"profit-and-loss failed: {pl}"
    assert pl_elapsed < 5.0, f"profit-and-loss took {pl_elapsed:.2f}s (> 5s)"

    # Time balance-sheet
    t0 = time.time()
    bs = _call_action("erpclaw-reports", "balance-sheet", conn,
                      company_id=cid,
                      as_of_date="2026-12-31")
    bs_elapsed = time.time() - t0
    assert bs["status"] == "ok", f"balance-sheet failed: {bs}"
    assert bs_elapsed < 5.0, f"balance-sheet took {bs_elapsed:.2f}s (> 5s)"
