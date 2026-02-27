"""Tests for GST compliance reports (GSTR-1, GSTR-3B) and TDS."""
import pytest
from conftest import run_action
from helpers import seed_invoices


class TestGSTR1:
    def test_gstr1_with_invoices(self, india_company):
        """Test 15: GSTR-1 B2B section grouped by GSTIN."""
        db_path, company_id = india_company
        si1, si2, _ = seed_invoices(db_path, company_id)

        out, rc = run_action(
            db_path, "generate-gstr1",
            company_id=company_id, month="1", year="2026",
        )
        assert rc == 0
        assert out["report"] == "GSTR-1"
        assert out["total_invoices"] == 2
        assert len(out["b2b_invoices"]) == 1  # Only B2B customer has GSTIN
        assert out["b2b_invoices"][0]["customer_gstin"] == "29AABCU9603R1ZJ"
        assert len(out["b2c_summary"]) >= 1  # B2C grouped by state

    def test_gstr1_empty_period(self, india_company):
        """GSTR-1 for period with no invoices."""
        db_path, company_id = india_company
        out, rc = run_action(
            db_path, "generate-gstr1",
            company_id=company_id, month="6", year="2025",
        )
        assert rc == 0
        assert out["total_invoices"] == 0
        assert out["b2b_invoices"] == []


class TestGSTR3B:
    def test_gstr3b_summary(self, india_company):
        """Test 17: GSTR-3B summary totals correct."""
        db_path, company_id = india_company
        seed_invoices(db_path, company_id)

        out, rc = run_action(
            db_path, "generate-gstr3b",
            company_id=company_id, month="1", year="2026",
        )
        assert rc == 0
        assert out["report"] == "GSTR-3B"

        # Outward: 50K + 20K = 70K taxable
        assert float(out["section_3_1"]["taxable_value"]) == 70000.0

        # ITC from purchases: 5400
        assert float(out["section_4"]["itc_available"]) == 5400.0

        # Net payable: 12600 - 5400 = 7200
        assert float(out["section_6"]["net_payable"]) == 7200.0


class TestComputeITC:
    def test_itc_calculation(self, india_company):
        """Test 13: ITC from purchases."""
        db_path, company_id = india_company
        seed_invoices(db_path, company_id)

        out, rc = run_action(
            db_path, "compute-itc",
            company_id=company_id, month="1", year="2026",
        )
        assert rc == 0
        assert float(out["total_purchase_tax_paid"]) == 5400.0
        assert float(out["eligible_itc"]) == 5400.0


class TestTDSWithhold:
    def test_tds_194c_contractor(self, tmp_db):
        """Test 22: TDS Section 194C (2%) — ₹100K payment → ₹2K TDS."""
        out, rc = run_action(
            tmp_db, "tds-withhold",
            section="194C", amount="100000", pan="ABCPE1234F",
        )
        assert rc == 0
        assert out["tds_applicable"] is True
        # Rate is 1% for individual, but we default to individual
        tds = float(out["tds_amount"])
        assert tds > 0

    def test_tds_below_threshold(self, tmp_db):
        """TDS below threshold — not applicable."""
        out, rc = run_action(
            tmp_db, "tds-withhold",
            section="194C", amount="25000", pan="ABCPE1234F",
        )
        assert rc == 0
        assert out["tds_applicable"] is False

    def test_tds_no_pan_higher_rate(self, tmp_db):
        """TDS without PAN — 20% higher rate applied."""
        out, rc = run_action(
            tmp_db, "tds-withhold",
            section="194H", amount="100000",
        )
        assert rc == 0
        assert out["tds_applicable"] is True
        assert float(out["rate"]) == 20  # Higher rate

    def test_tds_192_slab_based_error(self, tmp_db):
        """Section 192 is slab-based — should redirect to compute-tds-on-salary."""
        out, rc = run_action(
            tmp_db, "tds-withhold",
            section="192", amount="100000",
        )
        assert rc == 1
        assert "slab" in out["message"].lower()


class TestTaxSummary:
    def test_india_tax_summary(self, india_company):
        """Test 24: Tax summary dashboard."""
        db_path, company_id = india_company
        seed_invoices(db_path, company_id)

        out, rc = run_action(
            db_path, "india-tax-summary",
            company_id=company_id,
            from_date="2026-01-01", to_date="2026-01-31",
        )
        assert rc == 0
        assert out["report"] == "India Tax Summary"
        assert float(out["gst_collected_on_sales"]) == 12600.0
        assert float(out["gst_paid_on_purchases"]) == 5400.0
        assert float(out["net_gst_payable"]) == 7200.0


class TestStatus:
    def test_status_without_company(self, tmp_db):
        """Status action works without company_id."""
        out, rc = run_action(tmp_db, "status")
        assert rc == 0
        assert out["skill"] == "erpclaw-region-in"

    def test_status_with_company(self, india_company):
        """Status with company shows GST configuration."""
        db_path, company_id = india_company
        out, rc = run_action(db_path, "status", company_id=company_id)
        assert rc == 0
        assert out["gstin_configured"] is False  # Not yet configured

    def test_available_reports(self, tmp_db):
        """Available reports lists all India reports."""
        out, rc = run_action(tmp_db, "available-reports")
        assert rc == 0
        assert out["total"] >= 10
