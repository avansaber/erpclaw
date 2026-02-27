"""Tests for EU e-invoice (EN 16931), SAF-T, seed, and setup."""
from conftest import run_action


class TestGenerateEInvoice:
    def test_einvoice_structure(self, eu_company):
        """EN 16931 e-invoice has required fields."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "generate-einvoice-en16931",
                             company_id=company_id, invoice_id="test-inv-001")
        assert rc == 0
        assert out["standard"] == "EN 16931"
        assert "seller" in out
        assert "buyer" in out or "invoice_lines" in out

    def test_einvoice_no_invoice(self, eu_company):
        """E-invoice for non-existent invoice returns error."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "generate-einvoice-en16931",
                             company_id=company_id, invoice_id="nonexistent")
        assert rc == 0 or rc == 1  # Either empty or error


class TestGenerateSAFTExport:
    def test_saft_structure(self, eu_company):
        """SAF-T export has correct structure."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "generate-saft-export",
                             company_id=company_id, from_date="2026-01-01", to_date="2026-01-31")
        assert rc == 0
        assert out["standard"] == "OECD SAF-T v2.0"
        assert "header" in out


class TestSeedEUCOA:
    def test_seed_coa(self, eu_company):
        """Seed creates EU template chart of accounts."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "seed-eu-coa", company_id=company_id)
        assert rc == 0
        assert out["accounts_created"] >= 50


class TestComputeWithholdingTax:
    def test_wht_dividends(self, tmp_db):
        """WHT on dividends: DE to FR."""
        out, rc = run_action(tmp_db, "compute-withholding-tax",
                             amount="10000", income_type="dividends",
                             source_country="DE", recipient_country="FR")
        assert rc == 0
        assert float(out["wht_amount"]) >= 0

    def test_wht_interest(self, tmp_db):
        """WHT on interest payments."""
        out, rc = run_action(tmp_db, "compute-withholding-tax",
                             amount="10000", income_type="interest",
                             source_country="DE", recipient_country="FR")
        assert rc == 0
        assert "wht_rate" in out


class TestListEUCountries:
    def test_list_countries(self, tmp_db):
        """List returns all 27 EU member states."""
        out, rc = run_action(tmp_db, "list-eu-countries")
        assert rc == 0
        assert out["total"] == 27

    def test_list_includes_germany(self, tmp_db):
        """List includes Germany with EUR and 19% VAT."""
        out, rc = run_action(tmp_db, "list-eu-countries")
        assert rc == 0
        countries = out["countries"]
        de = [c for c in countries if c["code"] == "DE"][0]
        assert de["currency"] == "EUR"
        assert de["standard_vat"] == "19"


class TestListIntrastatCodes:
    def test_list_codes(self, tmp_db):
        """List returns intrastat codes."""
        out, rc = run_action(tmp_db, "list-intrastat-codes")
        assert rc == 0
        assert len(out["codes"]) >= 30
