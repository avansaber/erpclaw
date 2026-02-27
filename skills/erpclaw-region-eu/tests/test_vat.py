"""Tests for EU VAT computation, reverse charge, and OSS."""
from conftest import run_action


class TestComputeVAT:
    def test_vat_germany(self, tmp_db):
        """VAT 19% in Germany on EUR 1,000 = EUR 190."""
        out, rc = run_action(tmp_db, "compute-vat", amount="1000", country="DE")
        assert rc == 0
        assert out["vat_rate"] == "19"
        assert out["vat_amount"] == "190.00"
        assert out["total"] == "1190.00"

    def test_vat_france(self, tmp_db):
        """VAT 20% in France on EUR 1,000 = EUR 200."""
        out, rc = run_action(tmp_db, "compute-vat", amount="1000", country="FR")
        assert rc == 0
        assert out["vat_rate"] == "20"
        assert out["vat_amount"] == "200.00"

    def test_vat_hungary(self, tmp_db):
        """VAT 27% in Hungary on EUR 1,000 = EUR 270."""
        out, rc = run_action(tmp_db, "compute-vat", amount="1000", country="HU")
        assert rc == 0
        assert out["vat_rate"] == "27"

    def test_vat_reduced_rate(self, tmp_db):
        """Reduced VAT 7% in Germany on EUR 1,000 = EUR 70."""
        out, rc = run_action(tmp_db, "compute-vat", amount="1000", country="DE", rate_type="reduced")
        assert rc == 0
        assert out["vat_amount"] == "70.00"


class TestComputeReverseCharge:
    def test_intra_community(self, tmp_db):
        """Reverse charge: DE seller to FR buyer."""
        out, rc = run_action(tmp_db, "compute-reverse-charge",
                             amount="1000", seller_country="DE", buyer_country="FR")
        assert rc == 0
        assert out["seller_vat"] == "0.00"
        assert float(out["buyer_self_assessed_vat"]) > 0

    def test_same_country_no_rc(self, tmp_db):
        """Same country — not a reverse charge scenario."""
        out, rc = run_action(tmp_db, "compute-reverse-charge",
                             amount="1000", seller_country="DE", buyer_country="DE")
        assert rc == 0
        assert out["reverse_charge_applies"] is False


class TestListEUVATRates:
    def test_list_all_countries(self, tmp_db):
        """List returns all 27 member states."""
        out, rc = run_action(tmp_db, "list-eu-vat-rates")
        assert rc == 0
        assert out["total"] == 27

    def test_list_includes_germany(self, tmp_db):
        """List includes Germany at 19%."""
        out, rc = run_action(tmp_db, "list-eu-vat-rates")
        assert rc == 0
        rates = out["rates"]
        de = [r for r in rates if r["country"] == "DE"][0]
        assert de["standard"] == "19"


class TestComputeOSSVAT:
    def test_oss_b2c_digital(self, tmp_db):
        """OSS: German seller, French consumer — French rate applies."""
        out, rc = run_action(tmp_db, "compute-oss-vat",
                             amount="100", seller_country="DE", buyer_country="FR")
        assert rc == 0
        assert out["vat_rate"] == "20"
        assert out["vat_amount"] == "20.00"


class TestDistanceSelling:
    def test_below_threshold(self, tmp_db):
        """Below EUR 10K threshold — home country rate applies."""
        out, rc = run_action(tmp_db, "check-distance-selling-threshold",
                             annual_sales="5000")
        assert rc == 0
        assert out["threshold_exceeded"] is False

    def test_above_threshold(self, tmp_db):
        """Above EUR 10K threshold — must register or use OSS."""
        out, rc = run_action(tmp_db, "check-distance-selling-threshold",
                             annual_sales="15000")
        assert rc == 0
        assert out["threshold_exceeded"] is True


class TestTriangulation:
    def test_triangulation_check(self, tmp_db):
        """Three-party triangulation: A(DE) → B(FR) → C(IT)."""
        out, rc = run_action(tmp_db, "triangulation-check",
                             country_a="DE", country_b="FR", country_c="IT")
        assert rc == 0
        assert out["simplification_applies"] is True


class TestSeedDefaults:
    def test_seed_creates_accounts(self, eu_company):
        """Seed creates VAT accounts for member state."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "seed-eu-defaults", company_id=company_id)
        assert rc == 0
        assert out["accounts_created"] >= 3
        assert out["templates_created"] >= 2

    def test_seed_rejects_non_eu(self, non_eu_company):
        """Seed rejects non-EU company."""
        db_path, company_id = non_eu_company
        out, rc = run_action(db_path, "seed-eu-defaults", company_id=company_id)
        assert rc == 1


class TestSetupEUVAT:
    def test_setup_stores_vat(self, eu_company):
        """Setup stores EU VAT number."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "setup-eu-vat",
                             company_id=company_id, vat_number="DE123456789")
        assert rc == 0
        assert out["vat_number_stored"] is True

    def test_setup_invalid_vat(self, eu_company):
        """Setup rejects invalid EU VAT number."""
        db_path, company_id = eu_company
        out, rc = run_action(db_path, "setup-eu-vat",
                             company_id=company_id, vat_number="XX999")
        assert rc == 1
