"""Tests for ERPClaw OS configure-module and list-industries actions.

Uses a fresh temporary SQLite database with core schema seeded for each test.
Validates industry config structure, account creation, idempotency,
module recommendations, compliance items, and error handling.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_ERPCLAW_OS_DIR = _TESTS_DIR.parent
_SCRIPTS_DIR = _ERPCLAW_OS_DIR.parent       # erpclaw/scripts/
_ERPCLAW_DIR = _SCRIPTS_DIR.parent           # erpclaw/
_SRC_DIR = _ERPCLAW_DIR.parent               # src/
_PROJECT_ROOT = _SRC_DIR.parent              # project root
_SETUP_DIR = _SCRIPTS_DIR / "erpclaw-setup"
_INIT_SCHEMA_PATH = _SETUP_DIR / "init_schema.py"

# Ensure erpclaw_lib is importable
_ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if _ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, _ERPCLAW_LIB)

# Import modules from erpclaw-os (hyphenated dir, use importlib)
_configure_path = str(_ERPCLAW_OS_DIR / "configure_module.py")
_configure_spec = importlib.util.spec_from_file_location("configure_module", _configure_path)
_configure_mod = importlib.util.module_from_spec(_configure_spec)
_configure_spec.loader.exec_module(_configure_mod)
configure_module = _configure_mod.configure_module

_industry_path = str(_ERPCLAW_OS_DIR / "industry_configs.py")
_industry_spec = importlib.util.spec_from_file_location("industry_configs", _industry_path)
_industry_mod = importlib.util.module_from_spec(_industry_spec)
_industry_spec.loader.exec_module(_industry_mod)
INDUSTRY_CONFIGS = _industry_mod.INDUSTRY_CONFIGS
list_industries = _industry_mod.list_industries

# Load module_registry.json for validation
_REGISTRY_PATH = _SCRIPTS_DIR / "module_registry.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _init_all_tables(db_path: str):
    """Create all ERPClaw core tables using init_schema.init_db()."""
    spec = importlib.util.spec_from_file_location("init_schema", str(_INIT_SCHEMA_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db(db_path)


def _get_conn(db_path: str) -> sqlite3.Connection:
    """Return a configured sqlite3 connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _seed_company(conn, name="Test Co") -> str:
    """Insert a test company and return its ID."""
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month)
           VALUES (?, ?, ?, 'USD', 'United States', 1)""",
        (cid, name, f"TC{cid[:4]}")
    )
    conn.commit()
    return cid


def _seed_chart_of_accounts(conn, company_id: str):
    """Seed a minimal chart of accounts with the standard group accounts.

    Creates the parent group accounts that industry configs reference:
    Direct Income, Direct Expenses, Accounts Receivable, Accounts Payable,
    Fixed Assets, Stock Assets, Bank Accounts, Equity.
    """
    groups = [
        # (name, root_type, account_type, is_group)
        ("Direct Income", "income", "revenue", 1),
        ("Direct Expenses", "expense", "expense", 1),
        ("Accounts Receivable", "asset", "receivable", 1),
        ("Accounts Payable", "liability", "payable", 1),
        ("Fixed Assets", "asset", "fixed_asset", 1),
        ("Stock Assets", "asset", "stock", 1),
        ("Bank Accounts", "asset", "bank", 1),
        ("Equity", "equity", "equity", 1),
    ]
    for name, root_type, account_type, is_group in groups:
        direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
        conn.execute(
            """INSERT INTO account (id, name, root_type, account_type, is_group,
               balance_direction, company_id, depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (str(uuid.uuid4()), name, root_type, account_type, is_group,
             direction, company_id)
        )
    conn.commit()


@pytest.fixture
def db_path(tmp_path):
    """Per-test fresh SQLite database with full ERPClaw core schema."""
    path = str(tmp_path / "test.sqlite")
    _init_all_tables(path)
    yield path


@pytest.fixture
def conn(db_path):
    """Per-test database connection."""
    connection = _get_conn(db_path)
    yield connection
    connection.close()


@pytest.fixture
def company_id(conn):
    """Seed a company and return its ID."""
    return _seed_company(conn)


@pytest.fixture
def company_with_coa(conn, company_id):
    """Seed a company with standard chart of accounts group accounts."""
    _seed_chart_of_accounts(conn, company_id)
    return company_id


@pytest.fixture
def module_registry():
    """Load and return the module registry."""
    with open(_REGISTRY_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# TestIndustryConfigs — validate the config data structure
# ---------------------------------------------------------------------------

class TestIndustryConfigs:
    """Validate INDUSTRY_CONFIGS data integrity."""

    def test_all_configs_have_required_fields(self):
        """Every industry config must have display_name, accounts, modules, compliance_items."""
        for industry, config in INDUSTRY_CONFIGS.items():
            assert "display_name" in config, f"{industry}: missing display_name"
            assert "accounts" in config, f"{industry}: missing accounts"
            assert isinstance(config["accounts"], list), f"{industry}: accounts must be a list"
            assert len(config["accounts"]) > 0, f"{industry}: accounts must not be empty"
            assert "modules" in config, f"{industry}: missing modules"
            assert isinstance(config["modules"], dict), f"{industry}: modules must be a dict"
            assert "compliance_items" in config, f"{industry}: missing compliance_items"
            assert isinstance(config["compliance_items"], list), f"{industry}: compliance_items must be a list"
            assert len(config["compliance_items"]) > 0, f"{industry}: compliance_items must not be empty"

    def test_module_names_valid(self, module_registry):
        """All module names in configs must exist in module_registry.json."""
        valid_modules = set(module_registry["modules"].keys())
        for industry, config in INDUSTRY_CONFIGS.items():
            for tier, modules in config["modules"].items():
                for mod_name in modules:
                    assert mod_name in valid_modules, (
                        f"{industry}/{tier}: module '{mod_name}' not found in registry. "
                        f"Valid modules: {sorted(valid_modules)}"
                    )

    def test_size_tiers_present(self):
        """Every config must have at least 'small' tier in modules."""
        for industry, config in INDUSTRY_CONFIGS.items():
            assert "small" in config["modules"], (
                f"{industry}: missing 'small' tier in modules"
            )

    def test_all_size_tiers_are_valid(self):
        """All size tier keys must be from the valid set."""
        valid_tiers = {"small", "medium", "large", "enterprise"}
        for industry, config in INDUSTRY_CONFIGS.items():
            for tier in config["modules"]:
                assert tier in valid_tiers, (
                    f"{industry}: invalid tier '{tier}'. Must be one of {valid_tiers}"
                )

    def test_accounts_have_required_fields(self):
        """Every account entry must have name, root_type, and parent."""
        valid_root_types = {"asset", "liability", "equity", "income", "expense"}
        for industry, config in INDUSTRY_CONFIGS.items():
            for acct in config["accounts"]:
                assert "name" in acct, f"{industry}: account missing name"
                assert "root_type" in acct, f"{industry}/{acct['name']}: missing root_type"
                assert acct["root_type"] in valid_root_types, (
                    f"{industry}/{acct['name']}: invalid root_type '{acct['root_type']}'"
                )
                assert "parent" in acct, f"{industry}/{acct['name']}: missing parent"

    def test_larger_tiers_have_more_or_equal_modules(self):
        """Larger size tiers should recommend at least as many modules as smaller tiers."""
        tier_order = ["small", "medium", "large", "enterprise"]
        for industry, config in INDUSTRY_CONFIGS.items():
            prev_count = 0
            for tier in tier_order:
                if tier in config["modules"]:
                    count = len(config["modules"][tier])
                    assert count >= prev_count, (
                        f"{industry}: {tier} has {count} modules but previous tier had {prev_count}"
                    )
                    prev_count = count

    def test_at_least_10_industries(self):
        """Must have at least 10 industry configurations."""
        assert len(INDUSTRY_CONFIGS) >= 10, (
            f"Expected at least 10 industries, got {len(INDUSTRY_CONFIGS)}"
        )

    def test_no_duplicate_account_names_within_industry(self):
        """Account names must be unique within each industry."""
        for industry, config in INDUSTRY_CONFIGS.items():
            names = [a["name"] for a in config["accounts"]]
            assert len(names) == len(set(names)), (
                f"{industry}: duplicate account names found"
            )


# ---------------------------------------------------------------------------
# TestConfigureModule — functional tests with real DB
# ---------------------------------------------------------------------------

class TestConfigureModule:
    """Test configure_module() against a real temporary database."""

    def test_configure_dental_practice(self, db_path, company_with_coa):
        """Configure dental practice: verify accounts created, modules recommended."""
        result = configure_module(
            industry="dental_practice",
            company_id=company_with_coa,
            size_tier="small",
            db_path=db_path,
        )
        assert result["result"] == "pass"
        assert result["industry"] == "dental_practice"
        assert result["display_name"] == "Dental Practice"
        assert result["accounts_created"] > 0
        assert "healthclaw" in result["modules_recommended"]
        assert "healthclaw-dental" in result["modules_recommended"]
        assert len(result["compliance_items"]) > 0
        assert "HIPAA Privacy Rule Compliance" in result["compliance_items"]

    def test_configure_with_size_tiers(self, db_path, conn, company_with_coa):
        """Small dental vs large dental: large should recommend more modules."""
        result_small = configure_module(
            industry="dental_practice",
            company_id=company_with_coa,
            size_tier="small",
            db_path=db_path,
        )

        # Create a second company for large tier (to avoid account conflicts)
        large_company = _seed_company(conn, "Large Dental Corp")
        _seed_chart_of_accounts(conn, large_company)

        result_large = configure_module(
            industry="dental_practice",
            company_id=large_company,
            size_tier="large",
            db_path=db_path,
        )

        assert result_small["result"] == "pass"
        assert result_large["result"] == "pass"
        assert len(result_large["modules_recommended"]) > len(result_small["modules_recommended"])

    def test_configure_unknown_industry(self, db_path, company_with_coa):
        """Unknown industry should return error with list of available industries."""
        result = configure_module(
            industry="underwater_basket_weaving",
            company_id=company_with_coa,
            db_path=db_path,
        )
        assert result["result"] == "fail"
        assert "Unknown industry" in result["error"]
        assert "available_industries" in result
        assert len(result["available_industries"]) >= 10

    def test_configure_creates_accounts(self, db_path, conn, company_with_coa):
        """Verify industry-specific GL accounts are actually created in the DB."""
        result = configure_module(
            industry="dental_practice",
            company_id=company_with_coa,
            size_tier="small",
            db_path=db_path,
        )
        assert result["result"] == "pass"
        assert result["accounts_created"] > 0

        # Verify accounts exist in the database
        rows = conn.execute(
            "SELECT name FROM account WHERE company_id = ? AND is_group = 0",
            (company_with_coa,)
        ).fetchall()
        account_names = {row["name"] for row in rows}

        # Check that key dental accounts were created
        assert "Patient Revenue" in account_names
        assert "Lab Fees" in account_names
        assert "Dental Supplies" in account_names
        assert "Insurance Claims Receivable" in account_names

    def test_configure_idempotent(self, db_path, company_with_coa):
        """Running configure twice should not duplicate accounts."""
        result1 = configure_module(
            industry="dental_practice",
            company_id=company_with_coa,
            size_tier="small",
            db_path=db_path,
        )
        assert result1["result"] == "pass"
        first_created = result1["accounts_created"]
        assert first_created > 0

        result2 = configure_module(
            industry="dental_practice",
            company_id=company_with_coa,
            size_tier="small",
            db_path=db_path,
        )
        assert result2["result"] == "pass"
        assert result2["accounts_created"] == 0
        assert result2["accounts_skipped"] == first_created

    def test_configure_compliance_items(self, db_path, company_with_coa):
        """Verify compliance items are returned in the response."""
        result = configure_module(
            industry="law_firm",
            company_id=company_with_coa,
            size_tier="small",
            db_path=db_path,
        )
        assert result["result"] == "pass"
        assert len(result["compliance_items"]) > 0
        assert "IOLTA Trust Account Compliance" in result["compliance_items"]
        assert "State Bar License and Dues" in result["compliance_items"]

    def test_configure_requires_company_id(self, db_path):
        """Missing company_id should error."""
        result = configure_module(
            industry="dental_practice",
            company_id="",
            db_path=db_path,
        )
        assert result["result"] == "fail"
        assert "company_id" in result["error"].lower()

    def test_configure_invalid_size_tier(self, db_path, company_with_coa):
        """Invalid size tier should error."""
        result = configure_module(
            industry="dental_practice",
            company_id=company_with_coa,
            size_tier="mega",
            db_path=db_path,
        )
        assert result["result"] == "fail"
        assert "Invalid size_tier" in result["error"]

    def test_configure_nonexistent_company(self, db_path):
        """Configuring with a non-existent company ID should fail."""
        fake_id = str(uuid.uuid4())
        result = configure_module(
            industry="dental_practice",
            company_id=fake_id,
            db_path=db_path,
        )
        assert result["result"] == "fail"
        assert "not found" in result["error"].lower()

    def test_configure_general_contractor(self, db_path, company_with_coa):
        """Configure general contractor and verify construction-specific accounts."""
        result = configure_module(
            industry="general_contractor",
            company_id=company_with_coa,
            size_tier="medium",
            db_path=db_path,
        )
        assert result["result"] == "pass"
        assert "constructclaw" in result["modules_recommended"]
        assert result["accounts_created"] > 0

    def test_configure_manufacturing(self, db_path, company_with_coa):
        """Configure manufacturing and verify inventory-related accounts."""
        result = configure_module(
            industry="manufacturing",
            company_id=company_with_coa,
            size_tier="large",
            db_path=db_path,
        )
        assert result["result"] == "pass"
        assert "erpclaw-ops" in result["modules_recommended"]
        assert "erpclaw-planning" in result["modules_recommended"]
        assert result["accounts_created"] > 0

    def test_configure_restaurant(self, db_path, company_with_coa):
        """Configure restaurant and verify food-service accounts."""
        result = configure_module(
            industry="restaurant",
            company_id=company_with_coa,
            size_tier="small",
            db_path=db_path,
        )
        assert result["result"] == "pass"
        assert "foodclaw" in result["modules_recommended"]
        assert result["accounts_created"] > 0

    def test_configure_without_coa(self, db_path, company_id):
        """Configure without pre-existing chart of accounts still creates accounts.

        Accounts are created without parent_id when parent groups don't exist.
        """
        result = configure_module(
            industry="dental_practice",
            company_id=company_id,
            size_tier="small",
            db_path=db_path,
        )
        assert result["result"] == "pass"
        # Accounts still created, just without parent linkage
        assert result["accounts_created"] > 0

    def test_all_industries_configurable(self, db_path, conn):
        """Every industry in INDUSTRY_CONFIGS should configure without error."""
        for industry_key in INDUSTRY_CONFIGS:
            cid = _seed_company(conn, f"Company for {industry_key}")
            _seed_chart_of_accounts(conn, cid)
            result = configure_module(
                industry=industry_key,
                company_id=cid,
                size_tier="small",
                db_path=db_path,
            )
            assert result["result"] == "pass", (
                f"Failed to configure {industry_key}: {result.get('error')}"
            )
            assert result["accounts_created"] > 0, (
                f"{industry_key}: expected accounts to be created"
            )


# ---------------------------------------------------------------------------
# TestListIndustries — test the list-industries action
# ---------------------------------------------------------------------------

class TestListIndustries:
    """Test list_industries() function."""

    def test_list_industries(self):
        """Verify list-industries returns all available industry configs."""
        industries = list_industries()
        assert len(industries) >= 10
        keys = {i["industry"] for i in industries}
        assert "dental_practice" in keys
        assert "general_contractor" in keys
        assert "restaurant" in keys
        assert "law_firm" in keys
        assert "manufacturing" in keys

    def test_list_industries_has_required_fields(self):
        """Each industry summary must have industry, display_name, account_count, size_tiers."""
        industries = list_industries()
        for ind in industries:
            assert "industry" in ind
            assert "display_name" in ind
            assert "account_count" in ind
            assert "size_tiers" in ind
            assert "compliance_item_count" in ind
            assert ind["account_count"] > 0
            assert ind["compliance_item_count"] > 0
            assert "small" in ind["size_tiers"]

    def test_list_industries_sorted(self):
        """Industries should be returned in sorted order."""
        industries = list_industries()
        keys = [i["industry"] for i in industries]
        assert keys == sorted(keys)
