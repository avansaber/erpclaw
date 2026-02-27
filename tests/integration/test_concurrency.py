#!/usr/bin/env python3
"""Concurrency tests for ERPClaw — verifies correct behavior under
concurrent database access with WAL mode.

Uses ThreadPoolExecutor with separate connections per thread to simulate
multi-user concurrent access patterns.
"""
import json
import os
import uuid
import sqlite3
import shutil
import threading
import time
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

import pytest

from helpers import (
    _call_action,
    _run_init_db,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_account,
    create_test_cost_center,
)
from erpclaw_lib.db import _DecimalSum


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_conn(db_path):
    """Open a SQLite connection with ERPClaw standard PRAGMAs."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    return conn


def _setup_concurrent_env(db_path):
    """Initialize DB and create test environment for concurrency tests.

    Creates company, fiscal year, naming series, two bank accounts,
    and a cost center.  Returns dict of IDs.
    """
    _run_init_db(db_path)
    conn = _open_conn(db_path)

    cid = create_test_company(conn, name="Concurrency Corp", abbr="CC")
    fy_id = create_test_fiscal_year(conn, cid, name="FY 2026",
                                     start_date="2026-01-01",
                                     end_date="2026-12-31")
    seed_naming_series(conn, cid)

    bank_a = create_test_account(conn, cid, "Bank A", "asset",
                                  account_type="bank", account_number="1010")
    bank_b = create_test_account(conn, cid, "Bank B", "asset",
                                  account_type="bank", account_number="1020")
    cc = create_test_cost_center(conn, cid, name="Main")

    conn.close()
    return {
        "company_id": cid,
        "fy_id": fy_id,
        "bank_a": bank_a,
        "bank_b": bank_b,
        "cost_center_id": cc,
        "db_path": db_path,
    }


def _setup_inventory_env(db_path):
    """Initialize DB and create test environment for stock entry concurrency.

    Creates company, FY, naming series, item, warehouse with linked stock
    account, stock_received_not_billed account, COGS account, and cost center.
    Returns dict of IDs.
    """
    _run_init_db(db_path)
    conn = _open_conn(db_path)

    cid = create_test_company(conn, name="Inventory Corp", abbr="IC")
    fy_id = create_test_fiscal_year(conn, cid, name="FY 2026",
                                     start_date="2026-01-01",
                                     end_date="2026-12-31")
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid, name="Main")

    # Accounts needed for perpetual inventory GL
    stock_in_hand = create_test_account(conn, cid, "Stock In Hand", "asset",
                                         account_type="stock",
                                         account_number="1400")
    stock_received = create_test_account(conn, cid,
                                          "Stock Received Not Billed",
                                          "liability",
                                          account_type="stock_received_not_billed",
                                          account_number="2200")
    stock_adjustment = create_test_account(conn, cid, "Stock Adjustment",
                                            "expense",
                                            account_type="stock_adjustment",
                                            account_number="5200")
    cogs = create_test_account(conn, cid, "COGS", "expense",
                                account_type="cost_of_goods_sold",
                                account_number="5100")

    # Item
    item_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO item (id, item_code, item_name, item_type, stock_uom,
           valuation_method, standard_rate, has_batch, has_serial, status)
           VALUES (?, 'CTEST-001', 'Concurrent Widget', 'stock', 'Each',
                   'moving_average', '25.00', 0, 0, 'active')""",
        (item_id,),
    )
    conn.commit()

    # Warehouse with account_id linked
    wh_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO warehouse (id, name, warehouse_type, account_id,
           company_id, is_group)
           VALUES (?, 'Concurrent Warehouse', 'stores', ?, ?, 0)""",
        (wh_id, stock_in_hand, cid),
    )
    conn.commit()

    conn.close()
    return {
        "company_id": cid,
        "fy_id": fy_id,
        "cost_center_id": cc,
        "stock_in_hand": stock_in_hand,
        "stock_received": stock_received,
        "stock_adjustment": stock_adjustment,
        "cogs": cogs,
        "item_id": item_id,
        "warehouse_id": wh_id,
        "db_path": db_path,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.concurrency
class TestConcurrentJESubmits:
    """Test 1: Five journal entries submitted concurrently."""

    def test_concurrent_je_submits(self, tmp_path):
        db_path = str(tmp_path / "concurrent.sqlite")
        env = _setup_concurrent_env(db_path)

        # Create 5 draft JEs in main thread
        conn = _open_conn(db_path)
        je_ids = []
        for i in range(5):
            lines = json.dumps([
                {"account_id": env["bank_a"], "debit": "100.00", "credit": "0.00",
                 "cost_center_id": env["cost_center_id"]},
                {"account_id": env["bank_b"], "debit": "0.00", "credit": "100.00",
                 "cost_center_id": env["cost_center_id"]},
            ])
            r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=env["company_id"],
                              posting_date="2026-03-15",
                              entry_type="journal",
                              remark=f"Concurrent JE {i}",
                              lines=lines)
            assert r["status"] == "ok", f"Failed to create JE {i}: {r}"
            je_ids.append(r["journal_entry_id"])
        conn.close()

        # Submit all 5 concurrently — each thread opens its own connection
        results = {}
        errors = {}

        def _submit(je_id):
            c = _open_conn(db_path)
            try:
                r = _call_action("erpclaw-journals", "submit-journal-entry", c,
                                  journal_entry_id=je_id)
                return r
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_submit, jid): jid for jid in je_ids}
            for f in as_completed(futures):
                jid = futures[f]
                try:
                    results[jid] = f.result()
                except Exception as e:
                    errors[jid] = str(e)

        assert not errors, f"Submit errors: {errors}"

        # All 5 should have succeeded
        for jid, r in results.items():
            assert r["status"] == "ok", f"JE {jid} failed: {r}"

        # Verify GL: 10 entries (2 per JE), total debit == total credit
        conn = _open_conn(db_path)
        gl_count = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()[0]
        assert gl_count == 10, f"Expected 10 GL entries, got {gl_count}"

        totals = conn.execute(
            "SELECT decimal_sum(debit) as total_dr, decimal_sum(credit) as total_cr "
            "FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()
        total_dr = Decimal(totals["total_dr"])
        total_cr = Decimal(totals["total_cr"])
        assert total_dr == total_cr, f"Unbalanced: DR={total_dr} CR={total_cr}"
        assert total_dr == Decimal("500.00"), f"Expected 500.00, got {total_dr}"
        conn.close()


@pytest.mark.concurrency
class TestConcurrentNamingSeries:
    """Test 2: Naming series produces sequential values under concurrency."""

    def test_concurrent_naming_series(self, tmp_path):
        db_path = str(tmp_path / "naming.sqlite")
        env = _setup_concurrent_env(db_path)

        results = {}
        errors = {}

        def _create_je(thread_idx):
            c = _open_conn(db_path)
            try:
                lines = json.dumps([
                    {"account_id": env["bank_a"], "debit": "50.00", "credit": "0.00",
                     "cost_center_id": env["cost_center_id"]},
                    {"account_id": env["bank_b"], "debit": "0.00", "credit": "50.00",
                     "cost_center_id": env["cost_center_id"]},
                ])
                r = _call_action("erpclaw-journals", "add-journal-entry", c,
                                  company_id=env["company_id"],
                                  posting_date="2026-03-15",
                                  entry_type="journal",
                                  remark=f"Naming test {thread_idx}",
                                  lines=lines)
                return r
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_create_je, i): i for i in range(5)}
            for f in as_completed(futures):
                idx = futures[f]
                try:
                    results[idx] = f.result()
                except Exception as e:
                    errors[idx] = str(e)

        assert not errors, f"Creation errors: {errors}"

        # All 5 should succeed
        for idx, r in results.items():
            assert r["status"] == "ok", f"Thread {idx} failed: {r}"

        # Check naming_series current_value for journal_entry
        conn = _open_conn(db_path)
        ns = conn.execute(
            "SELECT current_value FROM naming_series "
            "WHERE entity_type = 'journal_entry' AND company_id = ?",
            (env["company_id"],),
        ).fetchone()
        assert ns is not None, "naming_series row for journal_entry not found"
        assert ns["current_value"] == 5, \
            f"Expected current_value=5, got {ns['current_value']}"

        # Verify 5 JEs exist with unique naming_series values
        jes = conn.execute(
            "SELECT naming_series FROM journal_entry WHERE company_id = ?",
            (env["company_id"],),
        ).fetchall()
        names = [row["naming_series"] for row in jes]
        assert len(names) == 5, f"Expected 5 JEs, got {len(names)}"
        assert len(set(names)) == 5, f"Duplicate naming_series: {names}"
        conn.close()


@pytest.mark.concurrency
class TestReadersDuringWrites:
    """Test 3: Readers get consistent snapshots while writers are active."""

    def test_readers_during_writes(self, tmp_path):
        db_path = str(tmp_path / "readwrite.sqlite")
        env = _setup_concurrent_env(db_path)

        # Pre-create and submit 2 JEs so readers have data
        conn = _open_conn(db_path)
        for i in range(2):
            lines = json.dumps([
                {"account_id": env["bank_a"], "debit": "200.00", "credit": "0.00",
                 "cost_center_id": env["cost_center_id"]},
                {"account_id": env["bank_b"], "debit": "0.00", "credit": "200.00",
                 "cost_center_id": env["cost_center_id"]},
            ])
            r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=env["company_id"],
                              posting_date="2026-03-15",
                              entry_type="journal",
                              remark=f"Pre-seed {i}",
                              lines=lines)
            assert r["status"] == "ok"
            r2 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                               journal_entry_id=r["journal_entry_id"])
            assert r2["status"] == "ok"
        conn.close()

        writer_results = []
        reader_results = []
        barrier = threading.Barrier(5, timeout=30)

        def _writer(thread_idx):
            """Create and submit a new JE."""
            barrier.wait()
            c = _open_conn(db_path)
            try:
                lines = json.dumps([
                    {"account_id": env["bank_a"], "debit": "100.00",
                     "credit": "0.00",
                     "cost_center_id": env["cost_center_id"]},
                    {"account_id": env["bank_b"], "debit": "0.00",
                     "credit": "100.00",
                     "cost_center_id": env["cost_center_id"]},
                ])
                r = _call_action("erpclaw-journals", "add-journal-entry", c,
                                  company_id=env["company_id"],
                                  posting_date="2026-03-15",
                                  entry_type="journal",
                                  remark=f"Writer {thread_idx}",
                                  lines=lines)
                if r["status"] == "ok":
                    r2 = _call_action(
                        "erpclaw-journals", "submit-journal-entry", c,
                        journal_entry_id=r["journal_entry_id"])
                    writer_results.append(("ok", r2))
                else:
                    writer_results.append(("create_fail", r))
            except Exception as e:
                writer_results.append(("error", str(e)))
            finally:
                c.close()

        def _reader(thread_idx):
            """Run trial-balance and verify it's balanced."""
            barrier.wait()
            c = _open_conn(db_path)
            try:
                r = _call_action("erpclaw-reports", "trial-balance", c,
                                  company_id=env["company_id"],
                                  from_date="2026-01-01",
                                  to_date="2026-12-31")
                reader_results.append(r)
            except Exception as e:
                reader_results.append({"error": str(e)})
            finally:
                c.close()

        threads = []
        for i in range(3):
            t = threading.Thread(target=_writer, args=(i,))
            threads.append(t)
        for i in range(2):
            t = threading.Thread(target=_reader, args=(i,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Writers should all succeed
        for status, detail in writer_results:
            assert status == "ok", f"Writer failed: {status} - {detail}"

        # Readers should return consistent results (no errors)
        for r in reader_results:
            assert "error" not in r, f"Reader error: {r}"
            assert r["status"] == "ok", f"Reader failed: {r}"

        # Final GL check: balanced
        conn = _open_conn(db_path)
        totals = conn.execute(
            "SELECT decimal_sum(debit) as total_dr, "
            "       decimal_sum(credit) as total_cr "
            "FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()
        total_dr = Decimal(totals["total_dr"])
        total_cr = Decimal(totals["total_cr"])
        assert total_dr == total_cr, f"Unbalanced: DR={total_dr} CR={total_cr}"
        conn.close()


@pytest.mark.concurrency
class TestConcurrentCancelAndSubmit:
    """Test 4: Cancel one JE while submitting another concurrently."""

    def test_concurrent_cancel_and_submit(self, tmp_path):
        db_path = str(tmp_path / "cancel_submit.sqlite")
        env = _setup_concurrent_env(db_path)

        # Create and submit one JE, create another in draft
        conn = _open_conn(db_path)

        # JE to cancel
        lines = json.dumps([
            {"account_id": env["bank_a"], "debit": "300.00", "credit": "0.00",
             "cost_center_id": env["cost_center_id"]},
            {"account_id": env["bank_b"], "debit": "0.00", "credit": "300.00",
             "cost_center_id": env["cost_center_id"]},
        ])
        r1 = _call_action("erpclaw-journals", "add-journal-entry", conn,
                            company_id=env["company_id"],
                            posting_date="2026-03-15",
                            entry_type="journal",
                            remark="To be cancelled",
                            lines=lines)
        assert r1["status"] == "ok"
        je_cancel_id = r1["journal_entry_id"]

        r1s = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                             journal_entry_id=je_cancel_id)
        assert r1s["status"] == "ok"

        # JE to submit
        lines2 = json.dumps([
            {"account_id": env["bank_a"], "debit": "150.00", "credit": "0.00",
             "cost_center_id": env["cost_center_id"]},
            {"account_id": env["bank_b"], "debit": "0.00", "credit": "150.00",
             "cost_center_id": env["cost_center_id"]},
        ])
        r2 = _call_action("erpclaw-journals", "add-journal-entry", conn,
                            company_id=env["company_id"],
                            posting_date="2026-03-15",
                            entry_type="journal",
                            remark="To be submitted",
                            lines=lines2)
        assert r2["status"] == "ok"
        je_submit_id = r2["journal_entry_id"]
        conn.close()

        # Concurrently: cancel one, submit the other
        cancel_result = {}
        submit_result = {}
        barrier = threading.Barrier(2, timeout=30)

        def _do_cancel():
            barrier.wait()
            c = _open_conn(db_path)
            try:
                r = _call_action("erpclaw-journals", "cancel-journal-entry", c,
                                  journal_entry_id=je_cancel_id)
                cancel_result["result"] = r
            except Exception as e:
                cancel_result["error"] = str(e)
            finally:
                c.close()

        def _do_submit():
            barrier.wait()
            c = _open_conn(db_path)
            try:
                r = _call_action("erpclaw-journals", "submit-journal-entry", c,
                                  journal_entry_id=je_submit_id)
                submit_result["result"] = r
            except Exception as e:
                submit_result["error"] = str(e)
            finally:
                c.close()

        t1 = threading.Thread(target=_do_cancel)
        t2 = threading.Thread(target=_do_submit)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert "error" not in cancel_result, \
            f"Cancel error: {cancel_result['error']}"
        assert "error" not in submit_result, \
            f"Submit error: {submit_result['error']}"
        assert cancel_result["result"]["status"] == "ok"
        assert submit_result["result"]["status"] == "ok"

        # Verify GL is balanced
        conn = _open_conn(db_path)

        # Original JE should be cancelled (reversal entries exist)
        cancelled_gl = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 1",
            (je_cancel_id,),
        ).fetchone()[0]
        reversal_gl = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
            (je_cancel_id,),
        ).fetchone()[0]
        # After cancel: original 2 marked cancelled, 2 reversal entries created
        assert cancelled_gl == 2, \
            f"Expected 2 cancelled GL entries, got {cancelled_gl}"
        assert reversal_gl == 2, \
            f"Expected 2 reversal GL entries, got {reversal_gl}"

        # New JE should have 2 GL entries
        new_gl = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
            (je_submit_id,),
        ).fetchone()[0]
        assert new_gl == 2, f"Expected 2 GL entries for new JE, got {new_gl}"

        # Overall balance: cancelled entries net to zero, new JE balanced
        totals = conn.execute(
            "SELECT decimal_sum(debit) as total_dr, "
            "       decimal_sum(credit) as total_cr "
            "FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()
        total_dr = Decimal(totals["total_dr"])
        total_cr = Decimal(totals["total_cr"])
        assert total_dr == total_cr, f"Unbalanced: DR={total_dr} CR={total_cr}"
        conn.close()


@pytest.mark.concurrency
class TestWALCheckpointUnderLoad:
    """Test 5: WAL checkpoint does not interfere with concurrent submits."""

    def test_wal_checkpoint_under_load(self, tmp_path):
        db_path = str(tmp_path / "wal_checkpoint.sqlite")
        env = _setup_concurrent_env(db_path)

        # Pre-create 3 draft JEs
        conn = _open_conn(db_path)
        je_ids = []
        for i in range(3):
            lines = json.dumps([
                {"account_id": env["bank_a"], "debit": "75.00", "credit": "0.00",
                 "cost_center_id": env["cost_center_id"]},
                {"account_id": env["bank_b"], "debit": "0.00", "credit": "75.00",
                 "cost_center_id": env["cost_center_id"]},
            ])
            r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=env["company_id"],
                              posting_date="2026-03-15",
                              entry_type="journal",
                              remark=f"WAL test {i}",
                              lines=lines)
            assert r["status"] == "ok"
            je_ids.append(r["journal_entry_id"])
        conn.close()

        submit_results = []
        checkpoint_results = []
        checkpoint_stop = threading.Event()

        def _submit_thread():
            """Submit JEs one by one."""
            for jid in je_ids:
                c = _open_conn(db_path)
                try:
                    r = _call_action(
                        "erpclaw-journals", "submit-journal-entry", c,
                        journal_entry_id=jid)
                    submit_results.append(r)
                except Exception as e:
                    submit_results.append({"error": str(e)})
                finally:
                    c.close()
            checkpoint_stop.set()

        def _checkpoint_thread():
            """Run WAL checkpoints repeatedly until told to stop."""
            while not checkpoint_stop.is_set():
                c = _open_conn(db_path)
                try:
                    result = c.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    checkpoint_results.append(
                        {"status": "ok", "result": dict(result)
                         if result else None})
                except Exception as e:
                    checkpoint_results.append({"error": str(e)})
                finally:
                    c.close()
                time.sleep(0.05)

        t1 = threading.Thread(target=_submit_thread)
        t2 = threading.Thread(target=_checkpoint_thread)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # All submissions should succeed
        for r in submit_results:
            assert "error" not in r, f"Submit error: {r}"
            assert r["status"] == "ok", f"Submit failed: {r}"

        # No checkpoint errors
        for r in checkpoint_results:
            assert "error" not in r, f"Checkpoint error: {r}"

        # Verify GL balanced
        conn = _open_conn(db_path)
        gl_count = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()[0]
        assert gl_count == 6, f"Expected 6 GL entries, got {gl_count}"

        totals = conn.execute(
            "SELECT decimal_sum(debit) as total_dr, "
            "       decimal_sum(credit) as total_cr "
            "FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()
        total_dr = Decimal(totals["total_dr"])
        total_cr = Decimal(totals["total_cr"])
        assert total_dr == total_cr, f"Unbalanced: DR={total_dr} CR={total_cr}"
        conn.close()


@pytest.mark.concurrency
class TestRapidConnectDisconnect:
    """Test 6: Rapid open/close of connections causes no resource leaks."""

    def test_rapid_connect_disconnect(self, tmp_path):
        db_path = str(tmp_path / "rapid_conn.sqlite")
        _run_init_db(db_path)

        # Set up minimal data so we can verify reads work
        conn = _open_conn(db_path)
        cid = create_test_company(conn, name="Rapid Corp", abbr="RC")
        conn.close()

        # Open and close 20 connections rapidly
        for i in range(20):
            c = _open_conn(db_path)
            # Quick read to ensure connection is functional
            row = c.execute(
                "SELECT COUNT(*) as cnt FROM company"
            ).fetchone()
            assert row["cnt"] >= 1
            c.close()

        # Verify final connection works correctly
        final_conn = _open_conn(db_path)
        row = final_conn.execute(
            "SELECT id, name FROM company WHERE id = ?", (cid,)
        ).fetchone()
        assert row is not None, "Company not found after rapid connect/disconnect"
        assert row["name"] == "Rapid Corp"

        # Verify integrity
        integrity = final_conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", f"Integrity check failed: {integrity}"
        final_conn.close()


@pytest.mark.concurrency
class TestConcurrentStockEntries:
    """Test 7: Five stock receive entries submitted concurrently."""

    def test_concurrent_stock_entries(self, tmp_path):
        db_path = str(tmp_path / "stock_concurrent.sqlite")
        env = _setup_inventory_env(db_path)

        # Create 5 draft stock entries in main thread
        conn = _open_conn(db_path)
        se_ids = []
        for i in range(5):
            items_json = json.dumps([{
                "item_id": env["item_id"],
                "qty": "10",
                "rate": "25.00",
                "to_warehouse_id": env["warehouse_id"],
            }])
            r = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                              entry_type="receive",
                              company_id=env["company_id"],
                              posting_date="2026-03-15",
                              items=items_json)
            assert r["status"] == "ok", f"Failed to create SE {i}: {r}"
            se_ids.append(r["stock_entry_id"])
        conn.close()

        # Submit all 5 concurrently
        results = {}
        errors = {}

        def _submit_se(se_id):
            c = _open_conn(db_path)
            try:
                r = _call_action("erpclaw-inventory", "submit-stock-entry", c,
                                  stock_entry_id=se_id)
                return r
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_submit_se, sid): sid for sid in se_ids}
            for f in as_completed(futures):
                sid = futures[f]
                try:
                    results[sid] = f.result()
                except Exception as e:
                    errors[sid] = str(e)

        assert not errors, f"Submit errors: {errors}"
        for sid, r in results.items():
            assert r["status"] == "ok", f"SE {sid} failed: {r}"

        # Verify total stock: 5 entries x 10 qty = 50 total actual_qty
        conn = _open_conn(db_path)
        sle_total = conn.execute(
            "SELECT decimal_sum(actual_qty) as total_qty "
            "FROM stock_ledger_entry "
            "WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0",
            (env["item_id"], env["warehouse_id"]),
        ).fetchone()
        total_qty = Decimal(sle_total["total_qty"])
        assert total_qty == Decimal("50"), \
            f"Expected total qty 50, got {total_qty}"

        # Verify GL is balanced (perpetual inventory GL entries)
        totals = conn.execute(
            "SELECT decimal_sum(debit) as total_dr, "
            "       decimal_sum(credit) as total_cr "
            "FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()
        total_dr = Decimal(totals["total_dr"])
        total_cr = Decimal(totals["total_cr"])
        assert total_dr == total_cr, f"Unbalanced GL: DR={total_dr} CR={total_cr}"
        conn.close()


@pytest.mark.concurrency
class TestConcurrentBackupDuringWrites:
    """Test 8: Backup (file copy) during writes produces valid DB."""

    def test_concurrent_backup_during_writes(self, tmp_path):
        db_path = str(tmp_path / "backup_writes.sqlite")
        backup_path = str(tmp_path / "backup_copy.sqlite")
        env = _setup_concurrent_env(db_path)

        # Pre-create a draft JE
        conn = _open_conn(db_path)
        lines = json.dumps([
            {"account_id": env["bank_a"], "debit": "500.00", "credit": "0.00",
             "cost_center_id": env["cost_center_id"]},
            {"account_id": env["bank_b"], "debit": "0.00", "credit": "500.00",
             "cost_center_id": env["cost_center_id"]},
        ])
        r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                          company_id=env["company_id"],
                          posting_date="2026-03-15",
                          entry_type="journal",
                          remark="Backup test",
                          lines=lines)
        assert r["status"] == "ok"
        je_id = r["journal_entry_id"]
        conn.close()

        submit_done = threading.Event()
        backup_result = {}
        submit_result = {}

        def _submit_thread():
            c = _open_conn(db_path)
            try:
                r = _call_action("erpclaw-journals", "submit-journal-entry", c,
                                  journal_entry_id=je_id)
                submit_result["result"] = r
            except Exception as e:
                submit_result["error"] = str(e)
            finally:
                c.close()
                submit_done.set()

        def _backup_thread():
            """Use sqlite3 backup API for a consistent copy."""
            try:
                src = sqlite3.connect(db_path)
                dst = sqlite3.connect(backup_path)
                src.backup(dst)
                src.close()
                dst.close()
                backup_result["ok"] = True
            except Exception as e:
                backup_result["error"] = str(e)

        t1 = threading.Thread(target=_submit_thread)
        t2 = threading.Thread(target=_backup_thread)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # Both should complete without error
        assert "error" not in submit_result, \
            f"Submit error: {submit_result['error']}"
        assert submit_result["result"]["status"] == "ok"
        assert "error" not in backup_result, \
            f"Backup error: {backup_result['error']}"

        # Verify the backup is a valid SQLite database
        bconn = sqlite3.connect(backup_path)
        integrity = bconn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", f"Backup integrity failed: {integrity}"

        # Backup should have the company table at minimum
        company_count = bconn.execute(
            "SELECT COUNT(*) FROM company"
        ).fetchone()[0]
        assert company_count >= 1, "Backup has no companies"
        bconn.close()


@pytest.mark.concurrency
class TestConcurrentJESameAccounts:
    """Test 9: Five JEs using identical accounts submitted concurrently."""

    def test_concurrent_je_same_accounts(self, tmp_path):
        db_path = str(tmp_path / "same_accts.sqlite")
        env = _setup_concurrent_env(db_path)

        # Create 5 draft JEs all using bank_a -> bank_b
        conn = _open_conn(db_path)
        je_ids = []
        for i in range(5):
            lines = json.dumps([
                {"account_id": env["bank_a"], "debit": "250.00",
                 "credit": "0.00",
                 "cost_center_id": env["cost_center_id"]},
                {"account_id": env["bank_b"], "debit": "0.00",
                 "credit": "250.00",
                 "cost_center_id": env["cost_center_id"]},
            ])
            r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=env["company_id"],
                              posting_date="2026-03-15",
                              entry_type="journal",
                              remark=f"Same accts {i}",
                              lines=lines)
            assert r["status"] == "ok"
            je_ids.append(r["journal_entry_id"])
        conn.close()

        # Submit all 5 concurrently
        results = {}
        errors = {}

        def _submit(je_id):
            c = _open_conn(db_path)
            try:
                r = _call_action("erpclaw-journals", "submit-journal-entry", c,
                                  journal_entry_id=je_id)
                return r
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_submit, jid): jid for jid in je_ids}
            for f in as_completed(futures):
                jid = futures[f]
                try:
                    results[jid] = f.result()
                except Exception as e:
                    errors[jid] = str(e)

        assert not errors, f"Submit errors: {errors}"
        for jid, r in results.items():
            assert r["status"] == "ok", f"JE {jid} failed: {r}"

        # 10 GL entries, all balanced, total debit = 1250
        conn = _open_conn(db_path)
        gl_count = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()[0]
        assert gl_count == 10, f"Expected 10 GL entries, got {gl_count}"

        totals = conn.execute(
            "SELECT decimal_sum(debit) as total_dr, "
            "       decimal_sum(credit) as total_cr "
            "FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()
        total_dr = Decimal(totals["total_dr"])
        total_cr = Decimal(totals["total_cr"])
        assert total_dr == total_cr, f"Unbalanced: DR={total_dr} CR={total_cr}"
        assert total_dr == Decimal("1250.00"), \
            f"Expected 1250.00, got {total_dr}"
        conn.close()


@pytest.mark.concurrency
class TestThreadSafetyDecimalSum:
    """Test 10: decimal_sum aggregate is thread-safe across connections."""

    def test_thread_safety_of_decimal_sum(self, tmp_path):
        db_path = str(tmp_path / "decimal_sum.sqlite")
        env = _setup_concurrent_env(db_path)

        # Create and submit 3 JEs to generate GL data
        conn = _open_conn(db_path)
        for i in range(3):
            amount = str(100 * (i + 1))  # 100, 200, 300
            lines = json.dumps([
                {"account_id": env["bank_a"], "debit": amount,
                 "credit": "0.00",
                 "cost_center_id": env["cost_center_id"]},
                {"account_id": env["bank_b"], "debit": "0.00",
                 "credit": amount,
                 "cost_center_id": env["cost_center_id"]},
            ])
            r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=env["company_id"],
                              posting_date="2026-03-15",
                              entry_type="journal",
                              remark=f"DecSum test {i}",
                              lines=lines)
            assert r["status"] == "ok"
            r2 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                               journal_entry_id=r["journal_entry_id"])
            assert r2["status"] == "ok"
        conn.close()

        # Expected totals: debit = 100+200+300 = 600, credit = 600
        expected_total = Decimal("600.00")

        results = {}
        errors = {}

        def _run_decimal_sum_query(thread_idx):
            c = _open_conn(db_path)
            try:
                row = c.execute(
                    "SELECT decimal_sum(debit) as total_dr, "
                    "       decimal_sum(credit) as total_cr "
                    "FROM gl_entry WHERE is_cancelled = 0"
                ).fetchone()
                return {
                    "total_dr": row["total_dr"],
                    "total_cr": row["total_cr"],
                }
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_run_decimal_sum_query, i): i
                       for i in range(5)}
            for f in as_completed(futures):
                idx = futures[f]
                try:
                    results[idx] = f.result()
                except Exception as e:
                    errors[idx] = str(e)

        assert not errors, f"Query errors: {errors}"

        # All 5 threads should get the same consistent result
        for idx, r in results.items():
            dr = Decimal(r["total_dr"])
            cr = Decimal(r["total_cr"])
            assert dr == expected_total, \
                f"Thread {idx}: expected DR {expected_total}, got {dr}"
            assert cr == expected_total, \
                f"Thread {idx}: expected CR {expected_total}, got {cr}"
            assert dr == cr, \
                f"Thread {idx}: unbalanced DR={dr} CR={cr}"
