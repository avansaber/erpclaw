"""Tests for Canadian compliance reports (GST/HST return, T4, ROE, tax summary)."""
from conftest import run_action
from helpers import create_test_employee, seed_invoices


class TestGSTHSTReturn:
    def test_return_with_invoices(self, ca_company):
        """GST/HST return with sales and purchase invoices."""
        db_path, company_id = ca_company
        seed_invoices(db_path, company_id)

        out, rc = run_action(
            db_path, "generate-gst-hst-return",
            company_id=company_id, period="1", year="2026",
        )
        assert rc == 0
        assert out["report"] == "GST/HST Return"
        # Total sales tax: 6500 + 2600 = 9100
        assert float(out["tax_collected"]) == 9100.0
        # ITC: 3900
        assert float(out["itc_claimed"]) == 3900.0
        # Net: 9100 - 3900 = 5200
        assert float(out["net_tax"]) == 5200.0

    def test_return_empty_period(self, ca_company):
        """GST/HST return for period with no invoices."""
        db_path, company_id = ca_company
        out, rc = run_action(
            db_path, "generate-gst-hst-return",
            company_id=company_id, period="6", year="2025",
        )
        assert rc == 0
        assert float(out["tax_collected"]) == 0
        assert float(out["net_tax"]) == 0


class TestQSTReturn:
    def test_qst_return_structure(self, qc_company):
        """QST return has correct structure."""
        db_path, company_id = qc_company
        out, rc = run_action(
            db_path, "generate-qst-return",
            company_id=company_id, period="1", year="2026",
        )
        assert rc == 0
        assert out["report"] == "QST Return"


class TestGenerateT4:
    def test_t4_structure(self, ca_company):
        """T4 slip has required fields."""
        db_path, company_id = ca_company
        emp_id = create_test_employee(db_path, company_id)

        out, rc = run_action(
            db_path, "generate-t4",
            employee_id=emp_id, tax_year="2026",
        )
        assert rc == 0
        assert out["form"] == "T4"
        assert "employee_name" in out
        assert "sin" in out or "sin_masked" in out

    def test_t4_no_employee(self, ca_company):
        """T4 for non-existent employee — error."""
        db_path, company_id = ca_company
        out, rc = run_action(
            db_path, "generate-t4",
            employee_id="nonexistent", tax_year="2026",
        )
        assert rc == 1


class TestGenerateROE:
    def test_roe_structure(self, ca_company):
        """ROE has required fields."""
        db_path, company_id = ca_company
        emp_id = create_test_employee(db_path, company_id)

        out, rc = run_action(db_path, "generate-roe", employee_id=emp_id)
        assert rc == 0
        assert out["form"] == "ROE"
        assert "employee_name" in out


class TestCATaxSummary:
    def test_summary_with_data(self, ca_company):
        """Tax summary dashboard with seeded data."""
        db_path, company_id = ca_company
        seed_invoices(db_path, company_id)

        out, rc = run_action(
            db_path, "ca-tax-summary",
            company_id=company_id, from_date="2026-01-01", to_date="2026-01-31",
        )
        assert rc == 0
        assert out["report"] == "Canada Tax Summary"
        assert float(out["gst_hst_collected"]) == 9100.0
        assert float(out["itc_paid"]) == 3900.0
        assert float(out["net_gst_hst_payable"]) == 5200.0


class TestAvailableReports:
    def test_lists_all_reports(self, tmp_db):
        """Available reports lists all Canadian reports."""
        out, rc = run_action(tmp_db, "available-reports")
        assert rc == 0
        assert out["total"] >= 8


class TestStatus:
    def test_status_without_company(self, tmp_db):
        """Status action works without company_id."""
        out, rc = run_action(tmp_db, "status")
        assert rc == 0
        assert out["skill"] == "erpclaw-region-ca"

    def test_status_with_company(self, ca_company):
        """Status with company shows GST/HST configuration."""
        db_path, company_id = ca_company
        out, rc = run_action(db_path, "status", company_id=company_id)
        assert rc == 0
