"""Tests for EU ID validation (VAT number, IBAN, EORI, VIES format)."""
from conftest import run_action


class TestValidateEUVATNumber:
    def test_valid_german_vat(self, tmp_db):
        """Valid German VAT: DE + 9 digits."""
        out, rc = run_action(tmp_db, "validate-eu-vat-number", vat_number="DE123456789")
        assert rc == 0
        assert out["valid"] is True
        assert out["country"] == "DE"

    def test_valid_french_vat(self, tmp_db):
        """Valid French VAT: FR + 2 chars + 9 digits."""
        out, rc = run_action(tmp_db, "validate-eu-vat-number", vat_number="FR12345678901")
        assert rc == 0
        assert out["valid"] is True

    def test_invalid_vat_unknown_country(self, tmp_db):
        """Unknown country prefix — rejected."""
        out, rc = run_action(tmp_db, "validate-eu-vat-number", vat_number="XX123456789")
        assert rc == 0
        assert out["valid"] is False

    def test_invalid_vat_wrong_length(self, tmp_db):
        """German VAT with wrong digit count — rejected."""
        out, rc = run_action(tmp_db, "validate-eu-vat-number", vat_number="DE12345")
        assert rc == 0
        assert out["valid"] is False


class TestValidateIBAN:
    def test_valid_german_iban(self, tmp_db):
        """Valid German IBAN (DE + 2 check + 18 digits = 22 chars)."""
        out, rc = run_action(tmp_db, "validate-iban", iban="DE89370400440532013000")
        assert rc == 0
        assert out["valid"] is True
        assert out["country"] == "DE"

    def test_invalid_iban_checksum(self, tmp_db):
        """IBAN with wrong check digits — rejected."""
        out, rc = run_action(tmp_db, "validate-iban", iban="DE00370400440532013000")
        assert rc == 0
        assert out["valid"] is False

    def test_valid_french_iban(self, tmp_db):
        """Valid French IBAN."""
        out, rc = run_action(tmp_db, "validate-iban", iban="FR7630006000011234567890189")
        assert rc == 0
        assert out["valid"] is True


class TestValidateEORI:
    def test_valid_eori(self, tmp_db):
        """Valid EORI: country prefix + up to 15 chars."""
        out, rc = run_action(tmp_db, "validate-eori", eori="DE123456789012345")
        assert rc == 0
        assert out["valid"] is True

    def test_invalid_eori_too_long(self, tmp_db):
        """EORI too long — rejected."""
        out, rc = run_action(tmp_db, "validate-eori", eori="DE1234567890123456")
        assert rc == 0
        assert out["valid"] is False


class TestCheckVIESFormat:
    def test_vies_format_germany(self, tmp_db):
        """VIES format check for German VAT number."""
        out, rc = run_action(tmp_db, "check-vies-format", vat_number="DE123456789")
        assert rc == 0
        assert out["format_valid"] is True
        assert out["country"] == "DE"
