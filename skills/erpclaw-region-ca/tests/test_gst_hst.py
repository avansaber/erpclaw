"""Tests for Canadian sales tax computation (GST/HST/PST/QST) and setup."""
from conftest import run_action
from helpers import create_test_purchase_invoice


class TestComputeGST:
    def test_gst_on_1000(self, tmp_db):
        """GST 5% on $1,000 = $50."""
        out, rc = run_action(tmp_db, "compute-gst", amount="1000")
        assert rc == 0
        assert out["gst_rate"] == "5"
        assert out["gst_amount"] == "50.00"
        assert out["total"] == "1050.00"

    def test_gst_small_amount(self, tmp_db):
        """GST 5% on $1.99 = $0.10."""
        out, rc = run_action(tmp_db, "compute-gst", amount="1.99")
        assert rc == 0
        assert out["gst_amount"] == "0.10"


class TestComputeHST:
    def test_hst_ontario(self, tmp_db):
        """HST 13% in Ontario on $1,000 = $130."""
        out, rc = run_action(tmp_db, "compute-hst", amount="1000", province="ON")
        assert rc == 0
        assert out["hst_rate"] == "13"
        assert out["hst_amount"] == "130.00"
        assert out["total"] == "1130.00"

    def test_hst_nova_scotia(self, tmp_db):
        """HST 14% in Nova Scotia on $500 = $70 (reduced from 15% effective 2025)."""
        out, rc = run_action(tmp_db, "compute-hst", amount="500", province="NS")
        assert rc == 0
        assert float(out["hst_amount"]) == 70.0

    def test_hst_non_hst_province_error(self, tmp_db):
        """HST in Alberta (non-HST) — error."""
        out, rc = run_action(tmp_db, "compute-hst", amount="1000", province="AB")
        assert rc == 1
        assert "hst" in out["message"].lower() or "not" in out["message"].lower()


class TestComputePST:
    def test_pst_bc(self, tmp_db):
        """PST 7% in BC on $1,000 = $70."""
        out, rc = run_action(tmp_db, "compute-pst", amount="1000", province="BC")
        assert rc == 0
        assert out["pst_amount"] == "70.00"

    def test_pst_saskatchewan(self, tmp_db):
        """PST 6% in Saskatchewan on $1,000 = $60."""
        out, rc = run_action(tmp_db, "compute-pst", amount="1000", province="SK")
        assert rc == 0
        assert out["pst_amount"] == "60.00"

    def test_pst_no_pst_province(self, tmp_db):
        """PST in Alberta (no PST) — returns 0."""
        out, rc = run_action(tmp_db, "compute-pst", amount="1000", province="AB")
        assert rc == 0
        assert out["pst_amount"] == "0.00"


class TestComputeQST:
    def test_qst_quebec(self, tmp_db):
        """QST 9.975% on $1,000 = $99.75."""
        out, rc = run_action(tmp_db, "compute-qst", amount="1000")
        assert rc == 0
        assert out["qst_rate"] == "9.975"
        assert out["qst_amount"] == "99.75"
        assert out["total"] == "1099.75"


class TestComputeSalesTax:
    def test_sales_tax_ontario(self, tmp_db):
        """Ontario: HST 13% only."""
        out, rc = run_action(tmp_db, "compute-sales-tax", amount="1000", province="ON")
        assert rc == 0
        assert out["tax_type"] == "HST"
        assert out["total_tax"] == "130.00"

    def test_sales_tax_alberta(self, tmp_db):
        """Alberta: GST 5% only (no provincial tax)."""
        out, rc = run_action(tmp_db, "compute-sales-tax", amount="1000", province="AB")
        assert rc == 0
        assert out["tax_type"] == "GST"
        assert out["total_tax"] == "50.00"

    def test_sales_tax_bc(self, tmp_db):
        """BC: GST 5% + PST 7% = 12%."""
        out, rc = run_action(tmp_db, "compute-sales-tax", amount="1000", province="BC")
        assert rc == 0
        assert out["gst_amount"] == "50.00"
        assert out["pst_amount"] == "70.00"
        assert out["total_tax"] == "120.00"

    def test_sales_tax_quebec(self, tmp_db):
        """Quebec: GST 5% + QST 9.975% = 14.975%."""
        out, rc = run_action(tmp_db, "compute-sales-tax", amount="1000", province="QC")
        assert rc == 0
        assert out["gst_amount"] == "50.00"
        assert out["qst_amount"] == "99.75"
        assert out["total_tax"] == "149.75"


class TestListTaxRates:
    def test_list_all_provinces(self, tmp_db):
        """List returns all 13 provinces/territories."""
        out, rc = run_action(tmp_db, "list-tax-rates")
        assert rc == 0
        assert out["total"] == 13


class TestSeedDefaults:
    def test_seed_creates_accounts(self, ca_company):
        """Seed creates GST/HST accounts and tax templates."""
        db_path, company_id = ca_company
        out, rc = run_action(db_path, "seed-ca-defaults", company_id=company_id)
        assert rc == 0
        assert out["accounts_created"] >= 4
        assert out["templates_created"] >= 3

    def test_seed_idempotent(self, ca_company):
        """Running seed twice doesn't create duplicates."""
        db_path, company_id = ca_company
        run_action(db_path, "seed-ca-defaults", company_id=company_id)
        out, rc = run_action(db_path, "seed-ca-defaults", company_id=company_id)
        assert rc == 0
        assert out["accounts_created"] == 0

    def test_seed_rejects_non_ca(self, us_company):
        """Seed rejects non-Canadian company."""
        db_path, company_id = us_company
        out, rc = run_action(db_path, "seed-ca-defaults", company_id=company_id)
        assert rc == 1


class TestSetupGSTHST:
    def test_setup_stores_bn(self, ca_company):
        """Setup stores Business Number in regional_settings."""
        db_path, company_id = ca_company
        out, rc = run_action(
            db_path, "setup-gst-hst",
            company_id=company_id, business_number="123456789RT0001",
            province="ON",
        )
        assert rc == 0
        assert out["business_number_stored"] is True

    def test_setup_invalid_bn(self, ca_company):
        """Setup rejects invalid BN format."""
        db_path, company_id = ca_company
        out, rc = run_action(
            db_path, "setup-gst-hst",
            company_id=company_id, business_number="INVALID",
            province="ON",
        )
        assert rc == 1


class TestComputeITC:
    def test_itc_from_purchases(self, ca_company):
        """ITC from submitted purchase invoices."""
        db_path, company_id = ca_company
        # Create a submitted purchase invoice with tax
        create_test_purchase_invoice(
            db_path, company_id, "PINV-2026-00001", "2026-01-15",
            "10000", "1300", "11300", docstatus=1,
        )

        out, rc = run_action(
            db_path, "compute-itc",
            company_id=company_id, month="1", year="2026",
        )
        assert rc == 0
        assert float(out["total_purchase_tax"]) == 1300.0
        assert float(out["eligible_itc"]) == 1300.0
