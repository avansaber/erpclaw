"""Tests for UK VAT computation, setup, and seed."""
from conftest import run_action


class TestComputeVAT:
    def test_vat_standard_rate(self, tmp_db):
        """VAT 20% on 1,000 = 200."""
        out, rc = run_action(tmp_db, "compute-vat", amount="1000", rate_type="standard")
        assert rc == 0
        assert out["vat_rate"] == "20"
        assert out["vat_amount"] == "200.00"
        assert out["total"] == "1200.00"

    def test_vat_reduced_rate(self, tmp_db):
        """VAT 5% on 1,000 = 50."""
        out, rc = run_action(tmp_db, "compute-vat", amount="1000", rate_type="reduced")
        assert rc == 0
        assert out["vat_rate"] == "5"
        assert out["vat_amount"] == "50.00"

    def test_vat_zero_rate(self, tmp_db):
        """VAT 0% on 1,000 = 0."""
        out, rc = run_action(tmp_db, "compute-vat", amount="1000", rate_type="zero")
        assert rc == 0
        assert out["vat_amount"] == "0.00"
        assert out["total"] == "1000.00"

    def test_vat_default_standard(self, tmp_db):
        """Default rate is standard (20%)."""
        out, rc = run_action(tmp_db, "compute-vat", amount="500")
        assert rc == 0
        assert out["vat_rate"] == "20"
        assert out["vat_amount"] == "100.00"

    def test_vat_small_amount(self, tmp_db):
        """VAT 20% on 1.99 = 0.40."""
        out, rc = run_action(tmp_db, "compute-vat", amount="1.99")
        assert rc == 0
        assert out["vat_amount"] == "0.40"


class TestComputeVATInclusive:
    def test_vat_inclusive_standard(self, tmp_db):
        """Reverse-calc VAT from 1,200 gross (20% VAT)."""
        out, rc = run_action(tmp_db, "compute-vat-inclusive", gross_amount="1200", rate_type="standard")
        assert rc == 0
        assert out["net_amount"] == "1000.00"
        assert out["vat_amount"] == "200.00"

    def test_vat_inclusive_reduced(self, tmp_db):
        """Reverse-calc VAT from 1,050 gross (5% VAT)."""
        out, rc = run_action(tmp_db, "compute-vat-inclusive", gross_amount="1050", rate_type="reduced")
        assert rc == 0
        assert out["net_amount"] == "1000.00"
        assert out["vat_amount"] == "50.00"


class TestComputeFlatRateVAT:
    def test_flat_rate_catering(self, tmp_db):
        """Flat rate 12.5% on 10,000 gross = 1,250 VAT."""
        out, rc = run_action(tmp_db, "compute-flat-rate-vat", gross_turnover="10000", category="Catering services incl restaurants & takeaways")
        assert rc == 0
        assert out["flat_rate"] == "12.5"
        assert out["vat_due"] == "1250.00"

    def test_flat_rate_first_year(self, tmp_db):
        """First year discount: 12.5% - 1% = 11.5%."""
        out, rc = run_action(tmp_db, "compute-flat-rate-vat", gross_turnover="10000", category="Catering services incl restaurants & takeaways", first_year="true")
        assert rc == 0
        assert out["flat_rate"] == "11.5"
        assert out["vat_due"] == "1150.00"


class TestListVATRates:
    def test_list_all_rates(self, tmp_db):
        """List returns standard, reduced, zero rates."""
        out, rc = run_action(tmp_db, "list-vat-rates")
        assert rc == 0
        assert out["standard_rate"] == "20"
        assert out["reduced_rate"] == "5"
        assert out["zero_rate"] == "0"


class TestSeedDefaults:
    def test_seed_creates_accounts(self, uk_company):
        """Seed creates VAT input/output accounts and tax templates."""
        db_path, company_id = uk_company
        out, rc = run_action(db_path, "seed-uk-defaults", company_id=company_id)
        assert rc == 0
        assert out["accounts_created"] >= 3
        assert out["templates_created"] >= 3

    def test_seed_idempotent(self, uk_company):
        """Running seed twice doesn't create duplicates."""
        db_path, company_id = uk_company
        run_action(db_path, "seed-uk-defaults", company_id=company_id)
        out, rc = run_action(db_path, "seed-uk-defaults", company_id=company_id)
        assert rc == 0
        assert out["accounts_created"] == 0

    def test_seed_rejects_non_uk(self, non_uk_company):
        """Seed rejects non-UK company."""
        db_path, company_id = non_uk_company
        out, rc = run_action(db_path, "seed-uk-defaults", company_id=company_id)
        assert rc == 1


class TestSetupVAT:
    def test_setup_stores_vat_number(self, uk_company):
        """Setup stores VAT number in regional_settings."""
        db_path, company_id = uk_company
        out, rc = run_action(db_path, "setup-vat", company_id=company_id, vat_number="GB123456789")
        assert rc == 0
        assert out["vat_number_stored"] is True

    def test_setup_invalid_vat(self, uk_company):
        """Setup rejects invalid VAT number."""
        db_path, company_id = uk_company
        out, rc = run_action(db_path, "setup-vat", company_id=company_id, vat_number="INVALID")
        assert rc == 1
