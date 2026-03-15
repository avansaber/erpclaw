"""Tests for erpclaw-setup company management actions.

Actions tested:
  - setup-company
  - update-company
  - get-company
  - list-companies
"""
import pytest
import uuid
from setup_helpers import call_action, ns, seed_company, is_error, is_ok, load_db_query

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# setup-company
# ──────────────────────────────────────────────────────────────────────────────

class TestSetupCompany:
    def test_basic_create(self, conn):
        """Create a company with just a name — auto-generates abbr, FY, CC, WH."""
        result = call_action(mod.setup_company, conn, ns(
            name="Acme Corp",
            abbr=None,
            currency=None,
            country=None,
            fiscal_year_start_month=None,
        ))
        assert result["name"] == "Acme Corp"
        assert result["abbr"] == "AC"
        assert "company_id" in result
        assert "fiscal_year_id" in result
        assert "cost_center_id" in result
        assert "warehouse_id" in result

        # Verify in DB
        row = conn.execute("SELECT * FROM company WHERE id = ?",
                           (result["company_id"],)).fetchone()
        assert row is not None
        assert row["name"] == "Acme Corp"
        assert row["default_currency"] == "USD"
        assert row["country"] == "United States"

    def test_custom_abbr_and_currency(self, conn):
        """Create company with custom abbreviation and currency."""
        result = call_action(mod.setup_company, conn, ns(
            name="Euro Trading",
            abbr="ET",
            currency="EUR",
            country="Germany",
            fiscal_year_start_month=4,
        ))
        assert result["abbr"] == "ET"
        row = conn.execute("SELECT * FROM company WHERE id = ?",
                           (result["company_id"],)).fetchone()
        assert row["default_currency"] == "EUR"
        assert row["country"] == "Germany"
        assert row["fiscal_year_start_month"] == 4

    def test_auto_creates_fiscal_year(self, conn):
        """setup-company should auto-create a fiscal year."""
        result = call_action(mod.setup_company, conn, ns(
            name="FY Test Co",
            abbr=None, currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        fy = conn.execute("SELECT * FROM fiscal_year WHERE id = ?",
                          (result["fiscal_year_id"],)).fetchone()
        assert fy is not None
        assert fy["company_id"] == result["company_id"]
        assert fy["is_closed"] == 0

    def test_auto_creates_cost_center(self, conn):
        """setup-company should auto-create a default cost center."""
        result = call_action(mod.setup_company, conn, ns(
            name="CC Test Co",
            abbr=None, currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        cc = conn.execute("SELECT * FROM cost_center WHERE id = ?",
                          (result["cost_center_id"],)).fetchone()
        assert cc is not None
        assert cc["company_id"] == result["company_id"]

        # Should be set as company default
        co = conn.execute("SELECT default_cost_center_id FROM company WHERE id = ?",
                          (result["company_id"],)).fetchone()
        assert co["default_cost_center_id"] == result["cost_center_id"]

    def test_auto_creates_warehouse(self, conn):
        """setup-company should auto-create a default warehouse."""
        result = call_action(mod.setup_company, conn, ns(
            name="WH Test Co",
            abbr=None, currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        wh = conn.execute("SELECT * FROM warehouse WHERE id = ?",
                          (result["warehouse_id"],)).fetchone()
        assert wh is not None
        assert wh["company_id"] == result["company_id"]

        # Should be set as company default
        co = conn.execute("SELECT default_warehouse_id FROM company WHERE id = ?",
                          (result["company_id"],)).fetchone()
        assert co["default_warehouse_id"] == result["warehouse_id"]

    def test_audit_log_created(self, conn):
        """setup-company should write an audit log entry."""
        result = call_action(mod.setup_company, conn, ns(
            name="Audit Test Co",
            abbr=None, currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        log = conn.execute(
            "SELECT * FROM audit_log WHERE entity_type='company' AND entity_id=?",
            (result["company_id"],)
        ).fetchone()
        assert log is not None
        assert log["action"] == "create"
        assert log["skill"] == "erpclaw-setup"

    def test_duplicate_name_fails(self, conn):
        """Cannot create two companies with the same name."""
        call_action(mod.setup_company, conn, ns(
            name="Unique Co",
            abbr="UC", currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        result = call_action(mod.setup_company, conn, ns(
            name="Unique Co",
            abbr="UC2", currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        assert is_error(result)

    def test_missing_name_fails(self, conn):
        """setup-company requires --name."""
        result = call_action(mod.setup_company, conn, ns(
            name=None,
            abbr=None, currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        assert is_error(result)

    def test_single_word_name_abbr(self, conn):
        """Single word name generates first-letter abbreviation."""
        result = call_action(mod.setup_company, conn, ns(
            name="Tesla",
            abbr=None, currency=None, country=None,
            fiscal_year_start_month=None,
        ))
        assert result["abbr"] == "T"


# ──────────────────────────────────────────────────────────────────────────────
# get-company
# ──────────────────────────────────────────────────────────────────────────────

class TestGetCompany:
    def test_get_by_id(self, conn):
        """Retrieve a specific company by ID."""
        cid = seed_company(conn, name="Get Test", abbr="GT")
        result = call_action(mod.get_company, conn, ns(company_id=cid))
        assert result["company"]["id"] == cid
        assert "Get Test" in result["company"]["name"]

    def test_get_default_company(self, conn):
        """If no company_id, returns first company."""
        cid = seed_company(conn)
        result = call_action(mod.get_company, conn, ns(company_id=None))
        assert "company" in result

    def test_get_nonexistent_fails(self, conn):
        """Getting a company with invalid ID returns error."""
        result = call_action(mod.get_company, conn, ns(
            company_id="nonexistent-id"
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# list-companies
# ──────────────────────────────────────────────────────────────────────────────

class TestListCompanies:
    def test_list_empty(self, conn):
        """List companies when none exist."""
        result = call_action(mod.list_companies, conn, ns(limit=None, offset=None))
        assert result["companies"] == []
        assert result["total_count"] == 0

    def test_list_multiple(self, conn):
        """List with multiple companies returns all."""
        seed_company(conn, name="Co A", abbr="CA")
        seed_company(conn, name="Co B", abbr="CB")
        seed_company(conn, name="Co C", abbr="CC")
        result = call_action(mod.list_companies, conn, ns(limit=None, offset=None))
        assert result["total_count"] == 3
        assert len(result["companies"]) == 3

    def test_list_pagination(self, conn):
        """Pagination with limit and offset."""
        for i in range(5):
            seed_company(conn, name=f"Page Co {i}", abbr=f"PC{i}")
        result = call_action(mod.list_companies, conn, ns(limit=2, offset=0))
        assert len(result["companies"]) == 2
        assert result["total_count"] == 5
        assert result["has_more"] is True


# ──────────────────────────────────────────────────────────────────────────────
# update-company
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateCompany:
    def test_update_name(self, conn):
        """Update a company's name."""
        cid = seed_company(conn, name="Old Name", abbr="ON")
        result = call_action(mod.update_company, conn, ns(
            company_id=cid,
            name="New Name",
            abbr=None, default_currency=None, country=None,
            tax_id=None, fiscal_year_start_month=None,
            default_receivable_account_id=None,
            default_payable_account_id=None,
            default_income_account_id=None,
            default_expense_account_id=None,
            default_cost_center_id=None,
            default_warehouse_id=None,
            default_bank_account_id=None,
            default_cash_account_id=None,
            round_off_account_id=None,
            exchange_gain_loss_account_id=None,
            perpetual_inventory=None,
            enable_negative_stock=None,
            accounts_frozen_till_date=None,
            role_allowed_for_frozen_entries=None,
        ))
        assert "updated_fields" in result
        assert "name" in result["updated_fields"]

        # Verify in DB
        row = conn.execute("SELECT name FROM company WHERE id = ?", (cid,)).fetchone()
        assert row["name"] == "New Name"

    def test_update_no_fields_fails(self, conn):
        """Update with no fields should error."""
        cid = seed_company(conn)
        result = call_action(mod.update_company, conn, ns(
            company_id=cid,
            name=None, abbr=None, default_currency=None, country=None,
            tax_id=None, fiscal_year_start_month=None,
            default_receivable_account_id=None,
            default_payable_account_id=None,
            default_income_account_id=None,
            default_expense_account_id=None,
            default_cost_center_id=None,
            default_warehouse_id=None,
            default_bank_account_id=None,
            default_cash_account_id=None,
            round_off_account_id=None,
            exchange_gain_loss_account_id=None,
            perpetual_inventory=None,
            enable_negative_stock=None,
            accounts_frozen_till_date=None,
            role_allowed_for_frozen_entries=None,
        ))
        assert is_error(result)

    def test_update_audit_logged(self, conn):
        """Update should create an audit log entry."""
        cid = seed_company(conn, name="Audit Update", abbr="AU")
        call_action(mod.update_company, conn, ns(
            company_id=cid,
            name="Audit Updated",
            abbr=None, default_currency=None, country=None,
            tax_id=None, fiscal_year_start_month=None,
            default_receivable_account_id=None,
            default_payable_account_id=None,
            default_income_account_id=None,
            default_expense_account_id=None,
            default_cost_center_id=None,
            default_warehouse_id=None,
            default_bank_account_id=None,
            default_cash_account_id=None,
            round_off_account_id=None,
            exchange_gain_loss_account_id=None,
            perpetual_inventory=None,
            enable_negative_stock=None,
            accounts_frozen_till_date=None,
            role_allowed_for_frozen_entries=None,
        ))
        log = conn.execute(
            "SELECT * FROM audit_log WHERE entity_type='company' AND action='update'"
        ).fetchone()
        assert log is not None
