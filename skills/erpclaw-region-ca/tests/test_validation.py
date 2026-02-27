"""Tests for Canadian ID validation (Business Number, SIN)."""
from conftest import run_action


class TestValidateBusinessNumber:
    def test_valid_bn_base(self, tmp_db):
        """Valid 9-digit Business Number."""
        out, rc = run_action(tmp_db, "validate-business-number", bn="123456789")
        assert rc == 0
        assert out["valid"] is True
        assert out["base_number"] == "123456789"

    def test_valid_bn_with_program(self, tmp_db):
        """Valid BN with program account (RT for GST/HST)."""
        out, rc = run_action(tmp_db, "validate-business-number", bn="123456789RT0001")
        assert rc == 0
        assert out["valid"] is True
        assert out["program_code"] == "RT"

    def test_invalid_bn_short(self, tmp_db):
        """BN too short — rejected."""
        out, rc = run_action(tmp_db, "validate-business-number", bn="12345")
        assert rc == 0
        assert out["valid"] is False

    def test_invalid_bn_letters(self, tmp_db):
        """BN base contains letters — rejected."""
        out, rc = run_action(tmp_db, "validate-business-number", bn="12345ABCD")
        assert rc == 0
        assert out["valid"] is False

    def test_invalid_program_code(self, tmp_db):
        """BN with invalid program code — rejected."""
        out, rc = run_action(tmp_db, "validate-business-number", bn="123456789XX0001")
        assert rc == 0
        assert out["valid"] is False


class TestValidateSIN:
    def test_valid_sin(self, tmp_db):
        """Valid SIN with correct Luhn checksum."""
        # 046 454 286 is a valid Luhn SIN
        out, rc = run_action(tmp_db, "validate-sin", sin="046454286")
        assert rc == 0
        assert out["valid"] is True

    def test_invalid_sin_checksum(self, tmp_db):
        """SIN with wrong Luhn checksum — rejected."""
        out, rc = run_action(tmp_db, "validate-sin", sin="046454287")
        assert rc == 0
        assert out["valid"] is False

    def test_invalid_sin_short(self, tmp_db):
        """SIN too short — rejected."""
        out, rc = run_action(tmp_db, "validate-sin", sin="12345")
        assert rc == 0
        assert out["valid"] is False

    def test_sin_starts_with_zero(self, tmp_db):
        """SIN starting with 0 — invalid (0 not issued)."""
        out, rc = run_action(tmp_db, "validate-sin", sin="012345678")
        assert rc == 0
        assert out["valid"] is False

    def test_sin_starts_with_eight(self, tmp_db):
        """SIN starting with 8 — valid (temporary resident)."""
        # 802 456 087 — Luhn valid
        out, rc = run_action(tmp_db, "validate-sin", sin="802456087")
        assert rc == 0
        assert out["valid"] is True
        assert "temporary" in out.get("note", "").lower()
