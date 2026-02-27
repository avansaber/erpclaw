"""Tests for UK ID validation (VAT number, UTR, NINO, CRN)."""
from conftest import run_action


class TestValidateVATNumber:
    def test_valid_vat_standard(self, tmp_db):
        """Valid GB VAT number (9 digits)."""
        out, rc = run_action(tmp_db, "validate-vat-number", vat_number="GB123456789")
        assert rc == 0
        assert out["valid"] is True

    def test_valid_vat_no_prefix(self, tmp_db):
        """Valid VAT number without GB prefix -- auto-adds."""
        out, rc = run_action(tmp_db, "validate-vat-number", vat_number="123456789")
        assert rc == 0
        assert out["valid"] is True
        assert out["formatted"] == "GB123456789"

    def test_invalid_vat_short(self, tmp_db):
        """VAT number too short -- rejected."""
        out, rc = run_action(tmp_db, "validate-vat-number", vat_number="GB12345")
        assert rc == 0
        assert out["valid"] is False

    def test_invalid_vat_letters(self, tmp_db):
        """VAT number with letters in digits -- rejected."""
        out, rc = run_action(tmp_db, "validate-vat-number", vat_number="GB12345ABCD")
        assert rc == 0
        assert out["valid"] is False


class TestValidateUTR:
    def test_valid_utr(self, tmp_db):
        """Valid 10-digit UTR."""
        out, rc = run_action(tmp_db, "validate-utr", utr="1234567890")
        assert rc == 0
        assert out["valid"] is True

    def test_invalid_utr_short(self, tmp_db):
        """UTR too short -- rejected."""
        out, rc = run_action(tmp_db, "validate-utr", utr="12345")
        assert rc == 0
        assert out["valid"] is False


class TestValidateNINO:
    def test_valid_nino(self, tmp_db):
        """Valid NINO: 2 letters + 6 digits + suffix."""
        out, rc = run_action(tmp_db, "validate-nino", nino="AB123456C")
        assert rc == 0
        assert out["valid"] is True

    def test_invalid_nino_format(self, tmp_db):
        """NINO with invalid prefix -- rejected."""
        out, rc = run_action(tmp_db, "validate-nino", nino="DA123456C")
        assert rc == 0
        assert out["valid"] is False

    def test_invalid_nino_short(self, tmp_db):
        """NINO too short -- rejected."""
        out, rc = run_action(tmp_db, "validate-nino", nino="AB1234")
        assert rc == 0
        assert out["valid"] is False


class TestValidateCRN:
    def test_valid_crn_numeric(self, tmp_db):
        """Valid numeric CRN (8 digits)."""
        out, rc = run_action(tmp_db, "validate-crn", crn="12345678")
        assert rc == 0
        assert out["valid"] is True

    def test_valid_crn_with_prefix(self, tmp_db):
        """Valid CRN with letter prefix (e.g., SC for Scotland)."""
        out, rc = run_action(tmp_db, "validate-crn", crn="SC123456")
        assert rc == 0
        assert out["valid"] is True

    def test_invalid_crn_short(self, tmp_db):
        """CRN too short -- rejected."""
        out, rc = run_action(tmp_db, "validate-crn", crn="1234")
        assert rc == 0
        assert out["valid"] is False
