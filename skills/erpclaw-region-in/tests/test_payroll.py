"""Tests for Indian payroll: PF, ESI, Professional Tax, TDS on Salary."""
import pytest
from conftest import run_action


class TestComputePF:
    def test_pf_basic_15k(self, tmp_db):
        """Test 26: Basic ₹15K — Employee ₹1,800 + Employer ₹1,800."""
        out, rc = run_action(tmp_db, "compute-pf", basic_salary="15000")
        assert rc == 0
        assert out["employee_pf_12_pct"] == "1800.00"
        assert out["capped"] is False
        # EPS capped at ₹1,250
        assert out["employer_eps"] == "1249.50" or out["employer_eps"] == "1250.00"

    def test_pf_basic_25k_capped(self, tmp_db):
        """Test 27: Basic ₹25K — capped at ₹15K ceiling."""
        out, rc = run_action(tmp_db, "compute-pf", basic_salary="25000")
        assert rc == 0
        assert out["capped"] is True
        assert out["pf_wage"] == "15000.00"
        assert out["employee_pf_12_pct"] == "1800.00"

    def test_pf_basic_10k(self, tmp_db):
        """Basic ₹10K — full PF on actual basic."""
        out, rc = run_action(tmp_db, "compute-pf", basic_salary="10000")
        assert rc == 0
        assert out["employee_pf_12_pct"] == "1200.00"
        assert out["capped"] is False


class TestComputeESI:
    def test_esi_gross_20k(self, tmp_db):
        """Test 28: Gross ₹20K — Employee ₹150 + Employer ₹650."""
        out, rc = run_action(tmp_db, "compute-esi", gross_salary="20000")
        assert rc == 0
        assert out["applicable"] is True
        assert out["employee_contribution"] == "150"
        assert out["employer_contribution"] == "650"

    def test_esi_gross_25k_not_applicable(self, tmp_db):
        """Test 29: Gross ₹25K — not applicable (above ₹21K ceiling)."""
        out, rc = run_action(tmp_db, "compute-esi", gross_salary="25000")
        assert rc == 0
        assert out["applicable"] is False
        assert out["employee_contribution"] == "0.00"

    def test_esi_at_ceiling(self, tmp_db):
        """Gross exactly ₹21K — still applicable."""
        out, rc = run_action(tmp_db, "compute-esi", gross_salary="21000")
        assert rc == 0
        assert out["applicable"] is True
        assert out["employee_contribution"] == "158"  # 21000 * 0.0075 rounded

    def test_esi_just_above_ceiling(self, tmp_db):
        """Gross ₹21,001 — not applicable."""
        out, rc = run_action(tmp_db, "compute-esi", gross_salary="21001")
        assert rc == 0
        assert out["applicable"] is False


class TestComputeProfessionalTax:
    def test_pt_maharashtra_male_15k(self, tmp_db):
        """Test 30: Maharashtra male ₹15K → ₹200/month."""
        out, rc = run_action(
            tmp_db, "compute-professional-tax",
            gross_salary="15000", state_code="27",
        )
        assert rc == 0
        assert out["applicable"] is True
        assert out["tax"] == "200"

    def test_pt_karnataka_30k(self, tmp_db):
        """Test 31: Karnataka ₹30K → ₹200/month."""
        out, rc = run_action(
            tmp_db, "compute-professional-tax",
            gross_salary="30000", state_code="29",
        )
        assert rc == 0
        assert out["tax"] == "200"

    def test_pt_karnataka_below_threshold(self, tmp_db):
        """Karnataka below ₹25K → ₹0."""
        out, rc = run_action(
            tmp_db, "compute-professional-tax",
            gross_salary="20000", state_code="29",
        )
        assert rc == 0
        assert out["tax"] == "0"

    def test_pt_andhra_20k(self, tmp_db):
        """Andhra Pradesh ₹20K → ₹150 or ₹200."""
        out, rc = run_action(
            tmp_db, "compute-professional-tax",
            gross_salary="20000", state_code="28",
        )
        assert rc == 0
        assert out["applicable"] is True
        assert int(out["tax"]) >= 150

    def test_pt_unknown_state(self, tmp_db):
        """Unknown state code returns not applicable."""
        out, rc = run_action(
            tmp_db, "compute-professional-tax",
            gross_salary="20000", state_code="99",
        )
        assert rc == 0
        assert out["applicable"] is False


class TestComputeTDSOnSalary:
    def test_tds_new_regime_10l(self, tmp_db):
        """Test 33: New regime ₹10L — zero tax (below ₹12L rebate threshold)."""
        out, rc = run_action(
            tmp_db, "compute-tds-on-salary",
            annual_income="1000000", regime="new",
        )
        assert rc == 0
        assert out["regime"] == "new"
        # ₹10L - ₹75K std ded = ₹9.25L taxable → below ₹12L rebate limit
        assert out["total_annual_tax"] == "0.00" or int(float(out["total_annual_tax"])) == 0

    def test_tds_new_regime_15l(self, tmp_db):
        """Test 34: New regime ₹15L — correct slab calculation."""
        out, rc = run_action(
            tmp_db, "compute-tds-on-salary",
            annual_income="1500000", regime="new",
        )
        assert rc == 0
        assert out["regime"] == "new"
        # Taxable = 15L - 75K = 14.25L
        # Tax: 0 on 4L + 5% on 4L(20K) + 10% on 4L(40K) + 15% on 2.25L(33750) = 93750
        # Above rebate limit, so no rebate
        # Cess: 93750 * 4% = 3750
        # Total: 97500
        total = float(out["total_annual_tax"])
        assert total > 90000 and total < 110000

    def test_tds_old_regime_8l(self, tmp_db):
        """Test 35: Old regime ₹8L — 5% on 2.5L-5L + 20% on 5L-8L."""
        out, rc = run_action(
            tmp_db, "compute-tds-on-salary",
            annual_income="800000", regime="old",
        )
        assert rc == 0
        assert out["regime"] == "old"
        # Taxable = 8L - 50K = 7.5L
        # Tax: 0 on 2.5L + 5% on 2.5L(12500) + 20% on 2.5L(50000) = 62500
        # Above 5L rebate limit (taxable 7.5L > 5L), no rebate
        # Cess: 62500 * 4% = 2500
        # Total: 65000
        total = float(out["total_annual_tax"])
        assert total > 60000 and total < 70000

    def test_tds_has_monthly_breakdown(self, tmp_db):
        """TDS result includes monthly TDS amount."""
        out, rc = run_action(
            tmp_db, "compute-tds-on-salary",
            annual_income="1500000", regime="new",
        )
        assert rc == 0
        assert "monthly_tds" in out
        monthly = int(out["monthly_tds"])
        annual = float(out["total_annual_tax"])
        # Monthly should be roughly annual / 12
        assert abs(monthly * 12 - annual) < 12  # Within rounding

    def test_tds_slab_breakdown(self, tmp_db):
        """TDS result includes slab-wise breakdown."""
        out, rc = run_action(
            tmp_db, "compute-tds-on-salary",
            annual_income="2000000", regime="new",
        )
        assert rc == 0
        assert "slab_breakdown" in out
        assert len(out["slab_breakdown"]) > 0

    def test_tds_new_regime_below_rebate(self, tmp_db):
        """New regime income below ₹12L → zero tax after rebate."""
        out, rc = run_action(
            tmp_db, "compute-tds-on-salary",
            annual_income="1200000", regime="new",
        )
        assert rc == 0
        # 12L - 75K = 11.25L taxable, which is ≤ 12L rebate limit
        total = float(out["total_annual_tax"])
        assert total == 0.0
