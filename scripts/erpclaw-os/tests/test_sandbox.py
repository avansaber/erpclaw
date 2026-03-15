"""Tests for the ERPClaw OS sandbox test runner and GL invariant checker.

TestGLInvariantChecker: validates GL invariant logic directly against
    in-memory SQLite databases with controlled GL entries.

TestSandboxRunner: validates the sandbox lifecycle — DB creation,
    init_schema seeding, module init_db, test execution, and cleanup.
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_ERPCLAW_OS_DIR = _TESTS_DIR.parent
_SCRIPTS_DIR = _ERPCLAW_OS_DIR.parent  # erpclaw/scripts/
_ERPCLAW_DIR = _SCRIPTS_DIR.parent     # erpclaw/
_SRC_DIR = _ERPCLAW_DIR.parent         # src/
_PROJECT_ROOT = _SRC_DIR.parent        # project root

# Ensure erpclaw_lib is importable
_ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if _ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, _ERPCLAW_LIB)

# Import gl_invariant_checker via importlib (directory has hyphen: erpclaw-os)
_gl_checker_path = str(_ERPCLAW_OS_DIR / "gl_invariant_checker.py")
_gl_spec = importlib.util.spec_from_file_location("gl_invariant_checker", _gl_checker_path)
_gl_mod = importlib.util.module_from_spec(_gl_spec)
_gl_spec.loader.exec_module(_gl_mod)
check_gl_invariants = _gl_mod.check_gl_invariants

# Import sandbox module via importlib (directory has hyphen: erpclaw-os)
_sandbox_path = str(_ERPCLAW_OS_DIR / "sandbox.py")
_spec = importlib.util.spec_from_file_location("sandbox_mod", _sandbox_path)
_sandbox_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sandbox_mod)
run_in_sandbox = _sandbox_mod.run_in_sandbox
_find_init_schema = _sandbox_mod._find_init_schema
_find_project_root = _sandbox_mod._find_project_root

# Core init_schema.py path
_INIT_SCHEMA_PATH = str(
    _PROJECT_ROOT / "src" / "erpclaw" / "scripts" / "erpclaw-setup" / "init_schema.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_minimal_gl_schema(db_path: str):
    """Create only the tables needed for GL invariant testing.

    This is a minimal subset: company, account, fiscal_year, cost_center, gl_entry.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS company (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cost_center (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            company_id TEXT NOT NULL REFERENCES company(id)
        );

        CREATE TABLE IF NOT EXISTS account (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            account_number  TEXT,
            parent_id       TEXT REFERENCES account(id),
            root_type       TEXT NOT NULL CHECK(root_type IN ('asset','liability','equity','income','expense')),
            account_type    TEXT,
            currency        TEXT NOT NULL DEFAULT 'USD',
            is_group        INTEGER NOT NULL DEFAULT 0,
            company_id      TEXT NOT NULL REFERENCES company(id),
            depth           INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS fiscal_year (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            start_date  TEXT NOT NULL,
            end_date    TEXT NOT NULL,
            is_closed   INTEGER NOT NULL DEFAULT 0,
            company_id  TEXT NOT NULL REFERENCES company(id)
        );

        CREATE TABLE IF NOT EXISTS gl_entry (
            id              TEXT PRIMARY KEY,
            posting_date    TEXT NOT NULL,
            created_at      TEXT DEFAULT (datetime('now')),
            account_id      TEXT NOT NULL REFERENCES account(id),
            party_type      TEXT,
            party_id        TEXT,
            debit           TEXT NOT NULL DEFAULT '0',
            credit          TEXT NOT NULL DEFAULT '0',
            currency        TEXT NOT NULL DEFAULT 'USD',
            debit_base      TEXT NOT NULL DEFAULT '0',
            credit_base     TEXT NOT NULL DEFAULT '0',
            exchange_rate   TEXT NOT NULL DEFAULT '1',
            voucher_type    TEXT NOT NULL,
            voucher_id      TEXT NOT NULL,
            entry_set       TEXT NOT NULL DEFAULT 'primary',
            cost_center_id  TEXT REFERENCES cost_center(id),
            project_id      TEXT,
            remarks         TEXT,
            fiscal_year     TEXT,
            is_cancelled    INTEGER NOT NULL DEFAULT 0,
            cancelled_by    TEXT
        );
    """)
    conn.commit()
    conn.close()


def _seed_company_and_accounts(db_path: str) -> dict:
    """Seed a company, two accounts, and a fiscal year. Returns IDs."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")

    company_id = str(uuid.uuid4())
    acct_cash_id = str(uuid.uuid4())
    acct_revenue_id = str(uuid.uuid4())
    fy_id = str(uuid.uuid4())
    fy_name = "FY-2026"

    conn.execute(
        "INSERT INTO company (id, name) VALUES (?, ?)",
        (company_id, "Test Co"),
    )
    conn.execute(
        "INSERT INTO account (id, name, root_type, account_type, company_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (acct_cash_id, "Cash", "asset", "cash", company_id),
    )
    conn.execute(
        "INSERT INTO account (id, name, root_type, account_type, company_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (acct_revenue_id, "Revenue", "income", "revenue", company_id),
    )
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (fy_id, fy_name, "2026-01-01", "2026-12-31", company_id),
    )
    conn.commit()
    conn.close()

    return {
        "company_id": company_id,
        "cash_id": acct_cash_id,
        "revenue_id": acct_revenue_id,
        "fy_id": fy_id,
        "fy_name": fy_name,
    }


def _insert_gl_entry(
    db_path: str,
    account_id: str,
    debit: str,
    credit: str,
    voucher_type: str = "journal_entry",
    voucher_id: str | None = None,
    fiscal_year: str | None = "FY-2026",
    is_cancelled: int = 0,
):
    """Insert a single gl_entry row."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")  # allow testing invalid refs
    entry_id = str(uuid.uuid4())
    voucher_id = voucher_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO gl_entry "
        "(id, posting_date, account_id, debit, credit, voucher_type, "
        "voucher_id, fiscal_year, is_cancelled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, "2026-03-15", account_id, debit, credit,
         voucher_type, voucher_id, fiscal_year, is_cancelled),
    )
    conn.commit()
    conn.close()
    return entry_id


def _init_core_schema(db_path: str):
    """Run core init_schema.py against a database."""
    spec = importlib.util.spec_from_file_location("init_schema", _INIT_SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db(db_path)


# ============================================================================
# GL Invariant Checker Tests
# ============================================================================


class TestGLInvariantChecker:
    """Tests for gl_invariant_checker.check_gl_invariants()."""

    def test_balanced_entries_pass(self, tmp_path):
        """Create balanced GL entries, verify checker returns pass."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        ids = _seed_company_and_accounts(db_path)

        voucher_id = str(uuid.uuid4())
        _insert_gl_entry(db_path, ids["cash_id"], "1000.00", "0",
                         voucher_id=voucher_id)
        _insert_gl_entry(db_path, ids["revenue_id"], "0", "1000.00",
                         voucher_id=voucher_id)

        result = check_gl_invariants(db_path)
        assert result["result"] == "pass"
        assert len(result["violations"]) == 0
        # Verify all 5 checks ran
        check_names = [c["name"] for c in result["checks"]]
        assert "global_balance" in check_names
        assert "per_voucher_balance" in check_names
        assert "no_zero_zero_entries" in check_names
        assert "valid_accounts" in check_names
        assert "valid_fiscal_year" in check_names

    def test_imbalanced_entries_fail(self, tmp_path):
        """Create imbalanced GL entries, verify checker catches it."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        ids = _seed_company_and_accounts(db_path)

        voucher_id = str(uuid.uuid4())
        _insert_gl_entry(db_path, ids["cash_id"], "1000.00", "0",
                         voucher_id=voucher_id)
        _insert_gl_entry(db_path, ids["revenue_id"], "0", "500.00",
                         voucher_id=voucher_id)

        result = check_gl_invariants(db_path)
        assert result["result"] == "fail"
        assert len(result["violations"]) > 0

        # Both global and per-voucher should fail
        check_map = {c["name"]: c["result"] for c in result["checks"]}
        assert check_map["global_balance"] == "fail"
        assert check_map["per_voucher_balance"] == "fail"

    def test_zero_zero_entries_detected(self, tmp_path):
        """Create a zero-debit zero-credit entry, verify caught."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        ids = _seed_company_and_accounts(db_path)

        _insert_gl_entry(db_path, ids["cash_id"], "0", "0")

        result = check_gl_invariants(db_path)
        assert result["result"] == "fail"

        check_map = {c["name"]: c["result"] for c in result["checks"]}
        assert check_map["no_zero_zero_entries"] == "fail"
        assert any("Zero-zero" in v for v in result["violations"])

    def test_invalid_account_detected(self, tmp_path):
        """Create a GL entry with nonexistent account_id, verify caught."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        ids = _seed_company_and_accounts(db_path)

        # Insert with a fake account_id (FKs disabled in helper)
        fake_account = str(uuid.uuid4())
        _insert_gl_entry(db_path, fake_account, "100.00", "0")

        result = check_gl_invariants(db_path)
        assert result["result"] == "fail"

        check_map = {c["name"]: c["result"] for c in result["checks"]}
        assert check_map["valid_accounts"] == "fail"
        assert any("Invalid account_id" in v for v in result["violations"])

    def test_invalid_fiscal_year_detected(self, tmp_path):
        """Create a GL entry with nonexistent fiscal year, verify caught."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        ids = _seed_company_and_accounts(db_path)

        voucher_id = str(uuid.uuid4())
        _insert_gl_entry(db_path, ids["cash_id"], "500.00", "0",
                         voucher_id=voucher_id, fiscal_year="FY-NONEXISTENT")
        _insert_gl_entry(db_path, ids["revenue_id"], "0", "500.00",
                         voucher_id=voucher_id, fiscal_year="FY-NONEXISTENT")

        result = check_gl_invariants(db_path)
        assert result["result"] == "fail"

        check_map = {c["name"]: c["result"] for c in result["checks"]}
        assert check_map["valid_fiscal_year"] == "fail"
        assert any("Invalid fiscal_year" in v for v in result["violations"])

    def test_empty_gl_skipped(self, tmp_path):
        """DB with no gl_entry rows returns 'skip'."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        _seed_company_and_accounts(db_path)

        result = check_gl_invariants(db_path)
        assert result["result"] == "skip"
        assert result.get("reason") == "no GL entries"

    def test_cancelled_entries_excluded(self, tmp_path):
        """Cancelled entries (is_cancelled=1) should not affect balance check."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        ids = _seed_company_and_accounts(db_path)

        # Balanced active entries
        v1 = str(uuid.uuid4())
        _insert_gl_entry(db_path, ids["cash_id"], "1000.00", "0", voucher_id=v1)
        _insert_gl_entry(db_path, ids["revenue_id"], "0", "1000.00", voucher_id=v1)

        # Imbalanced but cancelled entries — should be ignored
        v2 = str(uuid.uuid4())
        _insert_gl_entry(db_path, ids["cash_id"], "9999.00", "0",
                         voucher_id=v2, is_cancelled=1)

        result = check_gl_invariants(db_path)
        assert result["result"] == "pass"
        assert len(result["violations"]) == 0

    def test_no_gl_table_skips(self, tmp_path):
        """DB without gl_entry table returns 'skip'."""
        db_path = str(tmp_path / "empty.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE dummy (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()

        result = check_gl_invariants(db_path)
        assert result["result"] == "skip"
        assert "does not exist" in result.get("reason", "")

    def test_multiple_vouchers_mixed(self, tmp_path):
        """One balanced voucher + one imbalanced = fail with correct violation."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        ids = _seed_company_and_accounts(db_path)

        # Voucher 1: balanced
        v1 = str(uuid.uuid4())
        _insert_gl_entry(db_path, ids["cash_id"], "100.00", "0", voucher_id=v1)
        _insert_gl_entry(db_path, ids["revenue_id"], "0", "100.00", voucher_id=v1)

        # Voucher 2: imbalanced
        v2 = str(uuid.uuid4())
        _insert_gl_entry(db_path, ids["cash_id"], "200.00", "0", voucher_id=v2)
        _insert_gl_entry(db_path, ids["revenue_id"], "0", "150.00", voucher_id=v2)

        result = check_gl_invariants(db_path)
        assert result["result"] == "fail"

        check_map = {c["name"]: c["result"] for c in result["checks"]}
        assert check_map["per_voucher_balance"] == "fail"
        assert check_map["global_balance"] == "fail"

        # Exactly 1 voucher violation (for v2)
        voucher_violations = [v for v in result["violations"] if "Voucher" in v]
        assert len(voucher_violations) == 1
        assert v2 in voucher_violations[0]

    def test_decimal_precision(self, tmp_path):
        """Verify Decimal precision: 0.1 + 0.2 must equal 0.3 exactly."""
        db_path = str(tmp_path / "test.sqlite")
        _create_minimal_gl_schema(db_path)
        ids = _seed_company_and_accounts(db_path)

        v1 = str(uuid.uuid4())
        # Debit side: 0.1 + 0.2 = 0.3
        _insert_gl_entry(db_path, ids["cash_id"], "0.1", "0", voucher_id=v1)
        _insert_gl_entry(db_path, ids["cash_id"], "0.2", "0", voucher_id=v1)
        # Credit side: 0.3
        _insert_gl_entry(db_path, ids["revenue_id"], "0", "0.3", voucher_id=v1)

        result = check_gl_invariants(db_path)
        assert result["result"] == "pass", f"Decimal precision failed: {result['violations']}"


# ============================================================================
# Sandbox Runner Tests
# ============================================================================


class TestSandboxRunner:
    """Tests for sandbox.run_in_sandbox()."""

    def test_sandbox_creates_fresh_db(self, tmp_path):
        """Verify sandbox creates a new SQLite DB with core schema."""
        db_path = str(tmp_path / "fresh.sqlite")
        _init_core_schema(db_path)

        # Verify core tables exist
        conn = sqlite3.connect(db_path)
        tables = [
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        conn.close()

        # Core schema should create dozens of tables
        assert len(tables) > 20
        assert "gl_entry" in tables
        assert "account" in tables
        assert "company" in tables
        assert "fiscal_year" in tables
        assert "schema_version" in tables

    def test_sandbox_runs_module_init_db(self, tmp_path):
        """Verify sandbox runs the module's init_db.py on top of core schema."""
        db_path = str(tmp_path / "with_module.sqlite")
        _init_core_schema(db_path)

        # Create a fake module with init_db.py
        module_dir = tmp_path / "fake_module"
        module_dir.mkdir()
        init_db_py = module_dir / "init_db.py"
        init_db_py.write_text(
            "import sqlite3, sys\n"
            "db_path = sys.argv[1]\n"
            "conn = sqlite3.connect(db_path)\n"
            "conn.execute('CREATE TABLE IF NOT EXISTS fake_test_table "
            "(id TEXT PRIMARY KEY, name TEXT)')\n"
            "conn.commit()\n"
            "conn.close()\n"
        )

        # Create test dir with a passing test
        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        (test_dir / "test_fake.py").write_text(
            "import sqlite3, os\n"
            "def test_table_exists():\n"
            "    db = os.environ['ERPCLAW_DB_PATH']\n"
            "    conn = sqlite3.connect(db)\n"
            "    tables = [r[0] for r in conn.execute(\n"
            "        \"SELECT name FROM sqlite_master WHERE type='table'\"\n"
            "    ).fetchall()]\n"
            "    assert 'fake_test_table' in tables\n"
            "    conn.close()\n"
        )

        result = run_in_sandbox(str(module_dir))
        assert result["result"] == "pass", f"Unexpected result: {result}"
        assert result["tests_run"] >= 1
        assert result["tests_passed"] >= 1

    def test_sandbox_runs_tests(self, tmp_path):
        """Verify sandbox runs pytest and captures pass/fail counts."""
        module_dir = tmp_path / "test_module"
        module_dir.mkdir()

        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        (test_dir / "test_basic.py").write_text(
            "def test_one(): assert True\n"
            "def test_two(): assert True\n"
            "def test_three(): assert 1 + 1 == 2\n"
        )

        result = run_in_sandbox(str(module_dir))
        assert result["result"] == "pass"
        assert result["tests_run"] == 3
        assert result["tests_passed"] == 3
        assert result["tests_failed"] == 0

    def test_sandbox_captures_failures(self, tmp_path):
        """Verify sandbox correctly reports test failures."""
        module_dir = tmp_path / "fail_module"
        module_dir.mkdir()

        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        (test_dir / "test_fail.py").write_text(
            "def test_pass(): assert True\n"
            "def test_fail(): assert False, 'intentional failure'\n"
        )

        result = run_in_sandbox(str(module_dir))
        assert result["result"] == "fail"
        assert result["tests_passed"] == 1
        assert result["tests_failed"] == 1

    def test_sandbox_cleanup_on_success(self, tmp_path):
        """Verify temp directory is cleaned up when tests pass."""
        module_dir = tmp_path / "clean_module"
        module_dir.mkdir()

        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        (test_dir / "test_ok.py").write_text("def test_ok(): assert True\n")

        result = run_in_sandbox(str(module_dir))
        assert result["result"] == "pass"
        assert result["sandbox_path"] == "(cleaned up)"

    def test_sandbox_preserves_on_failure(self, tmp_path):
        """Verify temp directory is preserved when tests fail (for debugging)."""
        module_dir = tmp_path / "keep_module"
        module_dir.mkdir()

        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        (test_dir / "test_bad.py").write_text(
            "def test_bad(): assert False\n"
        )

        result = run_in_sandbox(str(module_dir))
        assert result["result"] == "fail"
        # Sandbox path should be a real directory, not cleaned up
        assert result["sandbox_path"] != "(cleaned up)"
        assert os.path.isdir(result["sandbox_path"])

        # Manual cleanup
        shutil.rmtree(result["sandbox_path"], ignore_errors=True)

    def test_sandbox_timeout(self, tmp_path):
        """Verify sandbox respects the timeout parameter."""
        module_dir = tmp_path / "slow_module"
        module_dir.mkdir()

        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        (test_dir / "test_slow.py").write_text(
            "import time\n"
            "def test_slow():\n"
            "    time.sleep(10)\n"
            "    assert True\n"
        )

        result = run_in_sandbox(str(module_dir), timeout=2)
        assert result["result"] == "error"
        assert "timed out" in result.get("error", "").lower()

        # Manual cleanup
        if result["sandbox_path"] != "(cleaned up)" and os.path.isdir(result["sandbox_path"]):
            shutil.rmtree(result["sandbox_path"], ignore_errors=True)

    def test_sandbox_no_test_dir_error(self, tmp_path):
        """Verify sandbox returns error when no test directory exists."""
        module_dir = tmp_path / "no_tests"
        module_dir.mkdir()

        result = run_in_sandbox(str(module_dir))
        assert result["result"] == "error"
        assert "test directory" in result.get("error", "").lower()

        # Manual cleanup
        if result["sandbox_path"] != "(cleaned up)" and os.path.isdir(result["sandbox_path"]):
            shutil.rmtree(result["sandbox_path"], ignore_errors=True)

    def test_sandbox_duration_tracked(self, tmp_path):
        """Verify duration_ms is populated."""
        module_dir = tmp_path / "timed_module"
        module_dir.mkdir()

        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        (test_dir / "test_time.py").write_text("def test_ok(): assert True\n")

        result = run_in_sandbox(str(module_dir))
        assert result["duration_ms"] > 0

    def test_sandbox_gl_invariants_checked(self, tmp_path):
        """Verify GL invariant check runs on sandbox DB after tests."""
        module_dir = tmp_path / "gl_module"
        module_dir.mkdir()

        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")

        # Write the inline test as a separate file for clarity
        gl_test_code = '''
import sqlite3, os, uuid

def test_gl_insert():
    db = os.environ["ERPCLAW_DB_PATH"]
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")

    # Create a company (abbr is NOT NULL in core schema)
    co_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country) "
        "VALUES (?, ?, ?, ?, ?)",
        (co_id, "TestCo", "TC", "USD", "United States"),
    )

    # Create accounts
    a1 = str(uuid.uuid4())
    a2 = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO account (id, name, root_type, account_type, "
        "company_id, balance_direction) VALUES (?, ?, ?, ?, ?, ?)",
        (a1, "Cash", "asset", "cash", co_id, "debit_normal"),
    )
    conn.execute(
        "INSERT INTO account (id, name, root_type, account_type, "
        "company_id, balance_direction) VALUES (?, ?, ?, ?, ?, ?)",
        (a2, "Revenue", "income", "revenue", co_id, "credit_normal"),
    )

    # Create fiscal year
    fy_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (fy_id, "FY-2026", "2026-01-01", "2026-12-31", co_id),
    )

    # Create balanced GL entries
    vid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        "voucher_type, voucher_id, fiscal_year, is_cancelled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), "2026-03-15", a1, "500.00", "0",
         "journal_entry", vid, "FY-2026", 0),
    )
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        "voucher_type, voucher_id, fiscal_year, is_cancelled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), "2026-03-15", a2, "0", "500.00",
         "journal_entry", vid, "FY-2026", 0),
    )

    conn.commit()
    conn.close()
'''
        (test_dir / "test_gl.py").write_text(gl_test_code)

        result = run_in_sandbox(str(module_dir))
        assert result["result"] == "pass", f"Unexpected: {result}"
        assert result["gl_invariants"]["result"] == "pass"

    def test_sandbox_keep_on_success(self, tmp_path):
        """Verify keep_on_success=True preserves directory."""
        module_dir = tmp_path / "keep_success"
        module_dir.mkdir()

        test_dir = module_dir / "scripts" / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        (test_dir / "test_ok.py").write_text("def test_ok(): assert True\n")

        result = run_in_sandbox(str(module_dir), keep_on_success=True)
        assert result["result"] == "pass"
        assert result["sandbox_path"] != "(cleaned up)"
        assert os.path.isdir(result["sandbox_path"])

        # Manual cleanup
        shutil.rmtree(result["sandbox_path"], ignore_errors=True)

    def test_project_root_discovery(self):
        """Verify _find_project_root locates the project correctly."""
        root = _find_project_root()
        assert (root / "src" / "erpclaw" / "scripts" / "erpclaw-setup" / "init_schema.py").exists()

    def test_find_init_schema(self):
        """Verify _find_init_schema returns a valid path."""
        path = _find_init_schema()
        assert os.path.isfile(path)
        assert path.endswith("init_schema.py")
