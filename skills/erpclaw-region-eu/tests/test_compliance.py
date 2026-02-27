"""Tests for EU compliance (VAT return, EC Sales, Intrastat, OSS return, summary)."""
import sqlite3
import uuid

from conftest import run_action


class TestGenerateVATReturn:
    def test_vat_return_with_invoices(self, eu_company):
        """VAT return from submitted invoices."""
        db_path, company_id = eu_company
        conn = sqlite3.connect(db_path)
        si_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO sales_invoice
               (id, name, company_id, posting_date, net_total, total_tax, grand_total, docstatus)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (si_id, "SINV-2026-00001", company_id, "2026-01-15", "10000", "1900", "11900", 1),
        )
        pi_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO purchase_invoice
               (id, name, company_id, posting_date, net_total, total_tax, grand_total, docstatus)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pi_id, "PINV-2026-00001", company_id, "2026-01-15", "5000", "950", "5950", 1),
        )
        conn.commit()
        conn.close()

        out, rc = run_action(db_path, "generate-vat-return",
                             company_id=company_id, period="1", year="2026")
        assert rc == 0
        assert out["report"] == "VAT Return"
        assert float(out["output_vat"]) == 1900.0
        assert float(out["input_vat"]) == 950.0

    def test_vat_return_empty(self, eu_company):
        """VAT return with no invoices."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "generate-vat-return",
                             company_id=company_id, period="1", year="2026")
        assert rc == 0
        assert float(out["net_vat"]) == 0


class TestGenerateECSalesList:
    def test_ec_sales_list(self, eu_company):
        """EC Sales List structure."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "generate-ec-sales-list",
                             company_id=company_id, period="1", year="2026")
        assert rc == 0
        assert out["report"] == "EC Sales List"
        assert "entries" in out


class TestGenerateIntrastat:
    def test_dispatches(self, eu_company):
        """Intrastat dispatches report."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "generate-intrastat-dispatches",
                             company_id=company_id, period="1", year="2026")
        assert rc == 0
        assert out["report"] == "Intrastat Dispatches"

    def test_arrivals(self, eu_company):
        """Intrastat arrivals report."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "generate-intrastat-arrivals",
                             company_id=company_id, period="1", year="2026")
        assert rc == 0
        assert out["report"] == "Intrastat Arrivals"


class TestGenerateOSSReturn:
    def test_oss_return(self, eu_company):
        """OSS quarterly return structure."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "generate-oss-return",
                             company_id=company_id, quarter="1", year="2026")
        assert rc == 0
        assert out["report"] == "OSS Return"


class TestEUTaxSummary:
    def test_tax_summary(self, eu_company):
        """Tax summary has required fields."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "eu-tax-summary",
                             company_id=company_id, from_date="2026-01-01", to_date="2026-01-31")
        assert rc == 0
        assert "domestic_vat_collected" in out
        assert "domestic_vat_paid" in out
        assert "net_vat" in out


class TestAvailableReports:
    def test_available_reports(self, tmp_db):
        """Lists all EU reports."""
        out, rc = run_action(tmp_db, "available-reports")
        assert rc == 0
        assert len(out["reports"]) >= 5


class TestStatus:
    def test_status(self, tmp_db):
        """Status returns skill info."""
        out, rc = run_action(tmp_db, "status")
        assert rc == 0
        assert out["skill"] == "erpclaw-region-eu"
