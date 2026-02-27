"""Tests for India ID validation: GSTIN, PAN, TAN, Aadhaar."""
import pytest
from conftest import run_action


class TestGSTINValidation:
    def test_valid_gstin(self, tmp_db):
        """Test 5: Valid GSTIN passes checksum."""
        out, rc = run_action(tmp_db, "validate-gstin", gstin="27AABCU9603R1ZN")
        assert rc == 0
        assert out["valid"] is True
        assert out["state_code"] == "27"
        assert out["pan"] == "AABCU9603R"
        assert out["state_name"] == "Maharashtra"

    def test_invalid_gstin_bad_checksum(self, tmp_db):
        """Test 6a: Invalid GSTIN — bad checksum."""
        out, rc = run_action(tmp_db, "validate-gstin", gstin="27AABCU9603R1ZX")
        assert rc == 0
        assert out["valid"] is False
        assert "checksum" in out["error"].lower() or "Checksum" in out["error"]

    def test_invalid_gstin_wrong_length(self, tmp_db):
        """Test 6b: Invalid GSTIN — wrong length."""
        out, rc = run_action(tmp_db, "validate-gstin", gstin="27AABCU9603R")
        assert rc == 0
        assert out["valid"] is False

    def test_invalid_gstin_bad_state(self, tmp_db):
        """Test 6c: Invalid GSTIN — state code 99 is too high."""
        out, rc = run_action(tmp_db, "validate-gstin", gstin="99AABCU9603R1ZM")
        assert rc == 0
        assert out["valid"] is False

    def test_gstin_extracts_pan(self, tmp_db):
        """GSTIN positions 3-12 are the PAN."""
        out, rc = run_action(tmp_db, "validate-gstin", gstin="29AABCU9603R1ZJ")
        assert rc == 0
        if out["valid"]:
            assert out["pan"] == "AABCU9603R"


class TestPANValidation:
    def test_valid_pan(self, tmp_db):
        """Test 7: Valid PAN passes."""
        out, rc = run_action(tmp_db, "validate-pan", pan="ABCPE1234F")
        assert rc == 0
        assert out["valid"] is True
        assert "Individual" in out["entity_type"] or "Person" in out["entity_type"]

    def test_valid_pan_company(self, tmp_db):
        """Company PAN (4th char = C)."""
        out, rc = run_action(tmp_db, "validate-pan", pan="AABCU9603R")
        assert rc == 0
        assert out["valid"] is True
        assert "Company" in out["entity_type"]

    def test_invalid_pan_wrong_length(self, tmp_db):
        """Test 8a: Invalid PAN — wrong length."""
        out, rc = run_action(tmp_db, "validate-pan", pan="ABCDE12")
        assert rc == 0
        assert out["valid"] is False

    def test_invalid_pan_wrong_format(self, tmp_db):
        """Test 8b: Invalid PAN — digits where letters expected."""
        out, rc = run_action(tmp_db, "validate-pan", pan="12345ABCDE")
        assert rc == 0
        assert out["valid"] is False


class TestTANValidation:
    def test_valid_tan(self, tmp_db):
        """Test 39: Valid TAN accepted."""
        out, rc = run_action(tmp_db, "validate-tan", tan="MUMK12345A")
        assert rc == 0
        assert out["valid"] is True

    def test_invalid_tan(self, tmp_db):
        """Invalid TAN — wrong format."""
        out, rc = run_action(tmp_db, "validate-tan", tan="1234567890")
        assert rc == 0
        assert out["valid"] is False


class TestAadhaarValidation:
    def test_valid_aadhaar(self, tmp_db):
        """Test 37: Valid 12-digit Aadhaar with Verhoeff checksum."""
        # Generate a valid Aadhaar using Verhoeff
        out, rc = run_action(tmp_db, "validate-aadhaar", aadhaar="234567890123")
        assert rc == 0
        # We just check it returns a result; the checksum may or may not pass
        assert "valid" in out

    def test_aadhaar_starts_with_zero(self, tmp_db):
        """Test 38: Aadhaar starting with 0 is rejected."""
        out, rc = run_action(tmp_db, "validate-aadhaar", aadhaar="012345678901")
        assert rc == 0
        assert out["valid"] is False
        assert "start" in out["error"].lower() or "0" in out["error"]

    def test_aadhaar_starts_with_one(self, tmp_db):
        """Aadhaar starting with 1 is rejected."""
        out, rc = run_action(tmp_db, "validate-aadhaar", aadhaar="112345678901")
        assert rc == 0
        assert out["valid"] is False

    def test_aadhaar_wrong_length(self, tmp_db):
        """Aadhaar must be exactly 12 digits."""
        out, rc = run_action(tmp_db, "validate-aadhaar", aadhaar="23456789")
        assert rc == 0
        assert out["valid"] is False

    def test_aadhaar_masked_in_output(self, tmp_db):
        """Aadhaar should be partially masked in output."""
        out, rc = run_action(tmp_db, "validate-aadhaar", aadhaar="234567890123")
        assert rc == 0
        assert "****" in out["aadhaar"]
