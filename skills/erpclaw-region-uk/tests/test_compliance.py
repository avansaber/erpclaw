"""Tests for UK compliance forms (VAT return, MTD, FPS, P60, P45, CIS)."""
from conftest import run_action
from helpers import (
    create_test_employee,
    create_test_purchase_invoice,
    create_test_salary_slip,
    create_test_sales_invoice,
)


class TestGenerateVATReturn:
    def test_vat_return_with_invoices(self, uk_company):
        """VAT return from submitted invoices."""
        db_path, company_id = uk_company
        # Create submitted sales invoice with VAT
        create_test_sales_invoice(
            db_path, company_id, "SINV-2026-00001", "2026-01-15",
            "10000", "2000", "12000", docstatus=1,
        )
        create_test_purchase_invoice(
            db_path, company_id, "PINV-2026-00001", "2026-01-15",
            "5000", "1000", "6000", docstatus=1,
        )

        out, rc = run_action(db_path, "generate-vat-return",
                             company_id=company_id, period="1", year="2026")
        assert rc == 0
        assert out["report"] == "VAT Return"
        assert float(out["box1_vat_due_sales"]) == 2000.0
        assert float(out["box4_vat_reclaimed"]) == 1000.0

    def test_vat_return_empty(self, uk_company):
        """VAT return with no invoices -- all zeros."""
        db_path, company_id = uk_company
        out, rc = run_action(db_path, "generate-vat-return",
                             company_id=company_id, period="1", year="2026")
        assert rc == 0
        assert float(out["box5_net_vat"]) == 0


class TestGenerateMTDPayload:
    def test_mtd_json_structure(self, uk_company):
        """MTD payload has correct HMRC structure."""
        db_path, company_id = uk_company
        out, rc = run_action(db_path, "generate-mtd-payload",
                             company_id=company_id, period="1", year="2026")
        assert rc == 0
        assert "periodKey" in out
        assert "vatDueSales" in out


class TestGenerateFPS:
    def test_fps_with_employee(self, uk_company):
        """FPS for an employee with salary data."""
        db_path, company_id = uk_company
        emp_id = create_test_employee(
            db_path, company_id, "John Smith", "John", "Smith",
            nino="AB123456C", status="active",
        )
        create_test_salary_slip(
            db_path, emp_id, company_id, "2026-01-31",
            "5000", "1500", "3500", payroll_period="2026-01", docstatus=1,
        )

        out, rc = run_action(db_path, "generate-fps",
                             company_id=company_id, month="1", year="2026")
        assert rc == 0
        assert out["form"] == "FPS"
        assert len(out["employees"]) >= 1


class TestGenerateP60:
    def test_p60_for_employee(self, uk_company):
        """P60 end-of-year certificate."""
        db_path, company_id = uk_company
        emp_id = create_test_employee(
            db_path, company_id, "Jane Doe", "Jane", "Doe",
            nino="CD987654A", status="active",
        )

        out, rc = run_action(db_path, "generate-p60",
                             employee_id=emp_id, tax_year="2025")
        assert rc == 0
        assert out["form"] == "P60"
        assert out["nino_masked"] is not None


class TestGenerateP45:
    def test_p45_for_leaver(self, uk_company):
        """P45 for leaving employee."""
        db_path, company_id = uk_company
        emp_id = create_test_employee(
            db_path, company_id, "Bob Jones", "Bob", "Jones",
            nino="EF456789B", date_of_leaving="2026-01-15", status="left",
        )

        out, rc = run_action(db_path, "generate-p45", employee_id=emp_id)
        assert rc == 0
        assert out["form"] == "P45"


class TestComputeCIS:
    def test_cis_standard(self, tmp_db):
        """CIS deduction at standard 20%."""
        out, rc = run_action(tmp_db, "compute-cis-deduction", amount="1000", cis_rate="standard")
        assert rc == 0
        assert out["deduction_rate"] == "20"
        assert out["deduction_amount"] == "200.00"
        assert out["net_payment"] == "800.00"

    def test_cis_higher(self, tmp_db):
        """CIS deduction at higher 30%."""
        out, rc = run_action(tmp_db, "compute-cis-deduction", amount="1000", cis_rate="higher")
        assert rc == 0
        assert out["deduction_rate"] == "30"
        assert out["deduction_amount"] == "300.00"

    def test_cis_gross(self, tmp_db):
        """CIS gross payment (verified subcontractor, 0%)."""
        out, rc = run_action(tmp_db, "compute-cis-deduction", amount="1000", cis_rate="gross")
        assert rc == 0
        assert out["deduction_rate"] == "0"
        assert out["net_payment"] == "1000.00"


class TestUKTaxSummary:
    def test_tax_summary(self, uk_company):
        """Tax summary returns all sections."""
        db_path, company_id = uk_company
        out, rc = run_action(db_path, "uk-tax-summary",
                             company_id=company_id, from_date="2026-01-01", to_date="2026-01-31")
        assert rc == 0
        assert "vat_collected" in out
        assert "vat_reclaimed" in out
        assert "net_vat" in out


class TestAvailableReports:
    def test_available_reports(self, tmp_db):
        """Lists all UK reports."""
        out, rc = run_action(tmp_db, "available-reports")
        assert rc == 0
        assert len(out["reports"]) >= 5


class TestStatus:
    def test_status(self, tmp_db):
        """Status returns skill info."""
        out, rc = run_action(tmp_db, "status")
        assert rc == 0
        assert out["skill"] == "erpclaw-region-uk"
