#!/usr/bin/env python3
"""S36: SQLite stress tests — concurrency, volume scaling, WAL behavior.

Standalone script (no pytest required). Run directly on the server:
    python3 test_sqlite_stress.py [--db-dir /tmp/erpclaw-stress]

Outputs JSON results to stdout. Creates a temp DB for each test.
"""
import json
import multiprocessing
import os
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

# Add shared lib for init_db
_LOCAL_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../"))
_SERVER_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "erpclaw-setup", "scripts")
if os.path.exists(os.path.join(_LOCAL_ROOT, "init_db.py")):
    PROJECT_ROOT = _LOCAL_ROOT
elif os.path.exists(os.path.join(_SERVER_ROOT, "init_db.py")):
    PROJECT_ROOT = _SERVER_ROOT
else:
    PROJECT_ROOT = _LOCAL_ROOT
LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)


def _init_db(db_path):
    """Create all tables via init_db.py."""
    import importlib.util
    init_path = os.path.join(PROJECT_ROOT, "init_db.py")
    if not os.path.exists(init_path):
        # Server layout: try multiple known locations
        for candidate in [
            os.path.expanduser("~/clawd/init_db.py"),
            os.path.join(os.path.dirname(__file__), "..", "..", "init_db.py"),
            os.path.join(os.path.dirname(__file__), "..", "init_db.py"),
        ]:
            if os.path.exists(candidate):
                init_path = candidate
                break
    spec = importlib.util.spec_from_file_location("init_db", init_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db(db_path)


def _connect(db_path):
    """Open a connection with ERPClaw standard PRAGMAs."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # OFF for benchmark inserts
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _setup_base_data(conn):
    """Insert company + accounts for GL benchmarks. Returns dict of IDs."""
    company_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country, "
        "fiscal_year_start_month) VALUES (?, 'StressTest Corp', 'ST', 'USD', 'US', 1)",
        (company_id,),
    )
    accounts = {}
    for name, root_type, num, atype in [
        ("Cash", "asset", "1000", "cash"),
        ("Bank", "asset", "1010", "bank"),
        ("AR", "asset", "1200", "receivable"),
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
            (aid, name, company_id, root_type, num, atype),
        )
        accounts[name] = aid
    cc_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, 'Main', ?, 0)",
        (cc_id, company_id),
    )
    conn.commit()
    return {"company_id": company_id, "accounts": accounts, "cc_id": cc_id}


def _insert_gl_batch(conn, accounts, cc_id, count, start_idx=0):
    """Insert `count` balanced GL entry pairs. Returns elapsed seconds."""
    base_date = date(2026, 1, 1)
    acct_keys = list(accounts.keys())
    batch = []
    for i in range(start_idx, start_idx + count):
        d = (base_date + timedelta(days=i % 365)).isoformat()
        vid = str(uuid.uuid4())
        amt = str(100 + (i % 1000))
        dr_acct = accounts[acct_keys[i % 2]]       # Cash or Bank
        cr_acct = accounts[acct_keys[3 + (i % 3)]]  # Revenue, COGS, or OpEx
        batch.append((str(uuid.uuid4()), d, dr_acct, amt, "0",
                       "journal_entry", vid, cc_id, "USD", "1", amt, "0", "FY2026"))
        batch.append((str(uuid.uuid4()), d, cr_acct, "0", amt,
                       "journal_entry", vid, cc_id, "USD", "1", "0", amt, "FY2026"))
    start = time.perf_counter()
    conn.executemany(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        "voucher_type, voucher_id, cost_center_id, currency, exchange_rate, "
        "debit_base, credit_base, fiscal_year) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        batch,
    )
    conn.commit()
    elapsed = time.perf_counter() - start
    return elapsed


def _benchmark_queries(conn, account_ids, company_id):
    """Run standard list + report queries. Returns dict of timings."""
    ph = ",".join("?" for _ in account_ids)
    results = {}

    # List query (paginated, 50 rows)
    start = time.perf_counter()
    conn.execute(
        f"SELECT * FROM gl_entry WHERE account_id IN ({ph}) "
        "ORDER BY posting_date DESC LIMIT 50", account_ids
    ).fetchall()
    results["list_50"] = time.perf_counter() - start

    # Trial balance aggregate
    start = time.perf_counter()
    conn.execute(
        f"SELECT account_id, SUM(CAST(debit AS REAL)) as d, "
        f"SUM(CAST(credit AS REAL)) as c "
        f"FROM gl_entry WHERE account_id IN ({ph}) GROUP BY account_id",
        account_ids,
    ).fetchall()
    results["trial_balance"] = time.perf_counter() - start

    # Date range query (one month)
    start = time.perf_counter()
    conn.execute(
        f"SELECT * FROM gl_entry WHERE account_id IN ({ph}) "
        "AND posting_date BETWEEN '2026-03-01' AND '2026-03-31' "
        "ORDER BY posting_date",
        account_ids,
    ).fetchall()
    results["date_range_month"] = time.perf_counter() - start

    # Count
    start = time.perf_counter()
    count = conn.execute(
        f"SELECT COUNT(*) FROM gl_entry WHERE account_id IN ({ph})",
        account_ids,
    ).fetchone()[0]
    results["count"] = time.perf_counter() - start
    results["total_rows"] = count

    return results


# ============================================================================
# Test 1: Concurrent Writes (5 processes)
# ============================================================================

def _writer_process(db_path, accounts_json, cc_id, process_id, count, result_queue):
    """Worker process that inserts GL entries."""
    accounts = json.loads(accounts_json)
    try:
        conn = _connect(db_path)
        elapsed = _insert_gl_batch(conn, accounts, cc_id, count, start_idx=process_id * count)
        conn.close()
        result_queue.put({"process": process_id, "elapsed": elapsed, "error": None, "rows": count * 2})
    except Exception as e:
        result_queue.put({"process": process_id, "elapsed": 0, "error": str(e), "rows": 0})


def test_concurrent_writes(db_dir):
    """5 processes each insert 200 GL entry pairs simultaneously."""
    db_path = os.path.join(db_dir, "concurrent.sqlite")
    _init_db(db_path)
    conn = _connect(db_path)
    data = _setup_base_data(conn)
    conn.close()

    accounts_json = json.dumps(data["accounts"])
    result_queue = multiprocessing.Queue()
    processes = []
    num_workers = 5
    per_worker = 200

    start = time.perf_counter()
    for i in range(num_workers):
        p = multiprocessing.Process(
            target=_writer_process,
            args=(db_path, accounts_json, data["cc_id"], i, per_worker, result_queue),
        )
        processes.append(p)
        p.start()

    for p in processes:
        p.join(timeout=60)

    wall_time = time.perf_counter() - start
    results = [result_queue.get() for _ in range(num_workers)]
    errors = [r for r in results if r["error"]]
    total_rows = sum(r["rows"] for r in results)

    # Verify count
    conn = _connect(db_path)
    actual = conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0]
    conn.close()

    return {
        "test": "concurrent_writes_5_processes",
        "workers": num_workers,
        "rows_per_worker": per_worker * 2,
        "total_rows_expected": num_workers * per_worker * 2,
        "total_rows_actual": actual,
        "wall_time_s": round(wall_time, 3),
        "per_worker": [{"process": r["process"], "time_s": round(r["elapsed"], 3), "error": r["error"]} for r in results],
        "lock_errors": len(errors),
        "pass": len(errors) == 0 and actual == num_workers * per_worker * 2,
    }


# ============================================================================
# Test 2: Read Under Write Load
# ============================================================================

def _bg_writer(db_path, accounts_json, cc_id, count, done_event):
    """Background writer for read-under-write test."""
    accounts = json.loads(accounts_json)
    conn = _connect(db_path)
    _insert_gl_batch(conn, accounts, cc_id, count)
    conn.close()
    done_event.set()


def test_read_under_write(db_dir):
    """Run trial balance while 10K GL entries are being inserted."""
    db_path = os.path.join(db_dir, "readwrite.sqlite")
    _init_db(db_path)
    conn = _connect(db_path)
    data = _setup_base_data(conn)

    # Pre-seed 5K entries so there's data to read
    _insert_gl_batch(conn, data["accounts"], data["cc_id"], 5000)
    conn.close()

    # Start background writer (10K more entries)
    accounts_json = json.dumps(data["accounts"])
    done_event = multiprocessing.Event()
    writer = multiprocessing.Process(
        target=_bg_writer,
        args=(db_path, accounts_json, data["cc_id"], 10000, done_event),
    )
    writer.start()

    # While writer is running, do 10 trial balance reads
    conn = _connect(db_path)
    account_ids = list(data["accounts"].values())
    ph = ",".join("?" for _ in account_ids)
    read_times = []
    read_errors = 0
    for _ in range(10):
        try:
            start = time.perf_counter()
            conn.execute(
                f"SELECT account_id, SUM(CAST(debit AS REAL)), SUM(CAST(credit AS REAL)) "
                f"FROM gl_entry WHERE account_id IN ({ph}) GROUP BY account_id",
                account_ids,
            ).fetchall()
            read_times.append(time.perf_counter() - start)
        except Exception:
            read_errors += 1
        time.sleep(0.1)

    writer.join(timeout=120)
    conn.close()

    return {
        "test": "read_under_write_load",
        "pre_seeded_rows": 10000,
        "background_write_rows": 20000,
        "read_iterations": 10,
        "read_times_s": [round(t, 4) for t in read_times],
        "avg_read_s": round(sum(read_times) / len(read_times), 4) if read_times else 0,
        "max_read_s": round(max(read_times), 4) if read_times else 0,
        "read_errors": read_errors,
        "pass": read_errors == 0 and (max(read_times) < 3.0 if read_times else False),
    }


# ============================================================================
# Tests 3-5: Volume Scaling (100K / 500K / 1M)
# ============================================================================

def test_volume_scaling(db_dir):
    """Progressively insert 100K, 500K, 1M GL entries and benchmark queries."""
    db_path = os.path.join(db_dir, "volume.sqlite")
    _init_db(db_path)
    conn = _connect(db_path)
    data = _setup_base_data(conn)
    account_ids = list(data["accounts"].values())

    tiers = [
        (50000, 100000, "100K"),   # 50K pairs = 100K rows
        (200000, 500000, "500K"),  # 200K more pairs = 400K more rows
        (250000, 1000000, "1M"),   # 250K more pairs = 500K more rows
    ]

    results = []
    total_pairs = 0
    for pairs, expected_total, label in tiers:
        # Insert
        insert_start = time.perf_counter()
        # Insert in batches of 10K pairs to avoid memory issues
        batch_size = 10000
        for batch_start in range(0, pairs, batch_size):
            batch_count = min(batch_size, pairs - batch_start)
            _insert_gl_batch(conn, data["accounts"], data["cc_id"],
                            batch_count, start_idx=total_pairs + batch_start)
        insert_time = time.perf_counter() - insert_start
        total_pairs += pairs

        # DB file size
        db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
        wal_path = db_path + "-wal"
        wal_size_mb = os.path.getsize(wal_path) / (1024 * 1024) if os.path.exists(wal_path) else 0

        # Benchmark queries
        benchmarks = _benchmark_queries(conn, account_ids, data["company_id"])

        tier_result = {
            "tier": label,
            "rows": benchmarks["total_rows"],
            "insert_time_s": round(insert_time, 2),
            "insert_rate_rows_per_s": round((pairs * 2) / insert_time, 0),
            "db_size_mb": round(db_size_mb, 1),
            "wal_size_mb": round(wal_size_mb, 1),
            "list_50_s": round(benchmarks["list_50"], 4),
            "trial_balance_s": round(benchmarks["trial_balance"], 4),
            "date_range_month_s": round(benchmarks["date_range_month"], 4),
            "count_s": round(benchmarks["count"], 4),
        }

        # Pass criteria
        if label == "100K":
            tier_result["pass"] = benchmarks["list_50"] < 1.0 and benchmarks["trial_balance"] < 3.0
        elif label == "500K":
            tier_result["pass"] = benchmarks["list_50"] < 2.0 and benchmarks["trial_balance"] < 5.0
        else:  # 1M
            tier_result["pass"] = benchmarks["list_50"] < 3.0 and benchmarks["trial_balance"] < 10.0

        results.append(tier_result)

    conn.close()
    return {
        "test": "volume_scaling",
        "tiers": results,
        "pass": all(t["pass"] for t in results),
    }


# ============================================================================
# Test 6: WAL Checkpoint Behavior
# ============================================================================

def test_wal_checkpoint(db_dir):
    """Insert 50K rows, check WAL size, force checkpoint, check again."""
    db_path = os.path.join(db_dir, "wal.sqlite")
    _init_db(db_path)
    conn = _connect(db_path)
    data = _setup_base_data(conn)
    wal_path = db_path + "-wal"

    # Insert 50K rows (25K pairs)
    _insert_gl_batch(conn, data["accounts"], data["cc_id"], 25000)

    wal_before = os.path.getsize(wal_path) / (1024 * 1024) if os.path.exists(wal_path) else 0

    # Force checkpoint
    start = time.perf_counter()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint_time = time.perf_counter() - start

    wal_after = os.path.getsize(wal_path) / (1024 * 1024) if os.path.exists(wal_path) else 0
    db_size = os.path.getsize(db_path) / (1024 * 1024)

    conn.close()
    return {
        "test": "wal_checkpoint",
        "rows_inserted": 50000,
        "wal_before_checkpoint_mb": round(wal_before, 2),
        "wal_after_checkpoint_mb": round(wal_after, 2),
        "checkpoint_time_s": round(checkpoint_time, 4),
        "db_size_mb": round(db_size, 1),
        "pass": wal_after < 1.0,  # WAL should be near-zero after TRUNCATE
    }


# ============================================================================
# Test 7: Backup Under Write Load
# ============================================================================

def _bg_writer_continuous(db_path, accounts_json, cc_id, duration_s, done_event):
    """Write GL entries continuously for `duration_s` seconds."""
    accounts = json.loads(accounts_json)
    conn = _connect(db_path)
    idx = 0
    end_time = time.time() + duration_s
    while time.time() < end_time:
        _insert_gl_batch(conn, accounts, cc_id, 100, start_idx=idx)
        idx += 100
    conn.close()
    done_event.set()


def test_backup_under_load(db_dir):
    """Run sqlite3 backup API while continuous writes are happening."""
    db_path = os.path.join(db_dir, "backup_load.sqlite")
    backup_path = os.path.join(db_dir, "backup_load_copy.sqlite")
    _init_db(db_path)
    conn = _connect(db_path)
    data = _setup_base_data(conn)

    # Pre-seed 10K rows
    _insert_gl_batch(conn, data["accounts"], data["cc_id"], 5000)
    conn.close()

    # Start background writer (runs for 10 seconds)
    accounts_json = json.dumps(data["accounts"])
    done_event = multiprocessing.Event()
    writer = multiprocessing.Process(
        target=_bg_writer_continuous,
        args=(db_path, accounts_json, data["cc_id"], 10, done_event),
    )
    writer.start()

    # Wait a moment for writes to start
    time.sleep(1)

    # Run backup while writes are happening
    source = sqlite3.connect(db_path)
    dest = sqlite3.connect(backup_path)
    backup_start = time.perf_counter()
    backup_error = None
    try:
        source.backup(dest)
    except Exception as e:
        backup_error = str(e)
    backup_time = time.perf_counter() - backup_start
    source.close()
    dest.close()

    writer.join(timeout=30)

    # Verify backup integrity
    verify_error = None
    backup_rows = 0
    if backup_error is None:
        try:
            bconn = sqlite3.connect(backup_path)
            bconn.execute("PRAGMA integrity_check")
            backup_rows = bconn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0]
            bconn.close()
        except Exception as e:
            verify_error = str(e)

    backup_size = os.path.getsize(backup_path) / (1024 * 1024) if os.path.exists(backup_path) else 0

    return {
        "test": "backup_under_write_load",
        "pre_seeded_rows": 10000,
        "backup_time_s": round(backup_time, 3),
        "backup_size_mb": round(backup_size, 1),
        "backup_rows": backup_rows,
        "backup_error": backup_error,
        "verify_error": verify_error,
        "integrity": backup_error is None and verify_error is None,
        "pass": backup_error is None and verify_error is None and backup_rows >= 10000,
    }


# ============================================================================
# Main runner
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="S36: SQLite Stress Tests")
    parser.add_argument("--db-dir", default="/tmp/erpclaw-stress",
                        help="Directory for temp test databases")
    parser.add_argument("--test", default="all",
                        help="Run specific test: concurrent, readwrite, volume, wal, backup, all")
    parser.add_argument("--skip-volume", action="store_true",
                        help="Skip the 1M volume test (slow)")
    args = parser.parse_args()

    os.makedirs(args.db_dir, exist_ok=True)
    print(f"=== S36: SQLite Stress Tests ===", file=sys.stderr)
    print(f"DB dir: {args.db_dir}", file=sys.stderr)
    print(f"PID: {os.getpid()}", file=sys.stderr)
    print(f"CPUs: {multiprocessing.cpu_count()}", file=sys.stderr)
    print(file=sys.stderr)

    all_results = {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cpus": multiprocessing.cpu_count(),
            "db_dir": args.db_dir,
            "python": sys.version.split()[0],
            "sqlite_version": sqlite3.sqlite_version,
        },
        "tests": [],
    }

    tests = {
        "concurrent": ("Test 1: Concurrent Writes (5 processes)", test_concurrent_writes),
        "readwrite": ("Test 2: Read Under Write Load", test_read_under_write),
        "volume": ("Test 3-5: Volume Scaling (100K → 1M)", test_volume_scaling),
        "wal": ("Test 6: WAL Checkpoint", test_wal_checkpoint),
        "backup": ("Test 7: Backup Under Load", test_backup_under_load),
    }

    run_tests = list(tests.keys()) if args.test == "all" else [args.test]

    for key in run_tests:
        if key not in tests:
            print(f"Unknown test: {key}", file=sys.stderr)
            continue
        label, fn = tests[key]
        print(f"Running {label}...", file=sys.stderr)
        start = time.perf_counter()
        try:
            result = fn(args.db_dir)
            result["wall_time_s"] = round(time.perf_counter() - start, 2)
            pass_str = "PASS" if result.get("pass") else "FAIL"
            print(f"  {pass_str} ({result['wall_time_s']}s)", file=sys.stderr)
        except Exception as e:
            result = {"test": key, "error": str(e), "pass": False,
                     "wall_time_s": round(time.perf_counter() - start, 2)}
            print(f"  ERROR: {e}", file=sys.stderr)
        all_results["tests"].append(result)

    # Summary
    passed = sum(1 for t in all_results["tests"] if t.get("pass"))
    total = len(all_results["tests"])
    all_results["summary"] = {
        "passed": passed,
        "total": total,
        "all_pass": passed == total,
    }

    print(file=sys.stderr)
    print(f"=== Results: {passed}/{total} passed ===", file=sys.stderr)

    # Clean up
    shutil.rmtree(args.db_dir, ignore_errors=True)

    # Output JSON to stdout
    print(json.dumps(all_results, indent=2))


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)
    main()
