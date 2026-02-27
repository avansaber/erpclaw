"""Tests for Canadian payroll deductions (CPP/QPP, EI, federal/provincial tax)."""
from conftest import run_action


class TestComputeCPP:
    def test_cpp_on_50k(self, tmp_db):
        """CPP on $50,000 annual salary."""
        out, rc = run_action(tmp_db, "compute-cpp", gross_salary="50000", pay_period="annual")
        assert rc == 0
        cpp = float(out["employee_cpp"])
        assert cpp > 0
        # Employer matches employee
        assert out["employee_cpp"] == out["employer_cpp"]

    def test_cpp_below_exemption(self, tmp_db):
        """CPP on $3,000 annual — below basic exemption ($3,500) = $0."""
        out, rc = run_action(tmp_db, "compute-cpp", gross_salary="3000", pay_period="annual")
        assert rc == 0
        assert float(out["employee_cpp"]) == 0

    def test_cpp_at_max(self, tmp_db):
        """CPP on $200,000 — capped at maximum pensionable earnings."""
        out, rc = run_action(tmp_db, "compute-cpp", gross_salary="200000", pay_period="annual")
        assert rc == 0
        cpp = float(out["employee_cpp"])
        max_cpp = float(out["annual_max_employee"])
        assert cpp == max_cpp

    def test_cpp_monthly(self, tmp_db):
        """CPP on monthly payroll ($5,000/month)."""
        out, rc = run_action(tmp_db, "compute-cpp", gross_salary="5000", pay_period="monthly")
        assert rc == 0
        assert float(out["employee_cpp"]) > 0


class TestComputeCPP2:
    def test_cpp2_in_range(self, tmp_db):
        """CPP2 on earnings between first and second ceiling."""
        out, rc = run_action(tmp_db, "compute-cpp2", annual_earnings="75000")
        assert rc == 0
        assert float(out["employee_cpp2"]) > 0

    def test_cpp2_below_first_ceiling(self, tmp_db):
        """CPP2 on earnings below first ceiling = $0."""
        out, rc = run_action(tmp_db, "compute-cpp2", annual_earnings="50000")
        assert rc == 0
        assert float(out["employee_cpp2"]) == 0

    def test_cpp2_above_second_ceiling(self, tmp_db):
        """CPP2 on earnings above second ceiling — capped."""
        out, rc = run_action(tmp_db, "compute-cpp2", annual_earnings="200000")
        assert rc == 0
        cpp2 = float(out["employee_cpp2"])
        max_cpp2 = float(out["annual_max_employee"])
        assert cpp2 == max_cpp2


class TestComputeQPP:
    def test_qpp_on_50k(self, tmp_db):
        """QPP on $50,000 — rate higher than CPP (6.40% vs 5.95%)."""
        out, rc = run_action(tmp_db, "compute-qpp", gross_salary="50000", pay_period="annual")
        assert rc == 0
        qpp = float(out["employee_qpp"])
        assert qpp > 0
        # QPP rate is higher than CPP
        assert float(out["rate"]) > 5.95


class TestComputeEI:
    def test_ei_on_50k(self, tmp_db):
        """EI on $50,000 annual salary."""
        out, rc = run_action(tmp_db, "compute-ei", gross_salary="50000", pay_period="annual")
        assert rc == 0
        employee_ei = float(out["employee_ei"])
        employer_ei = float(out["employer_ei"])
        assert employee_ei > 0
        # Employer pays 1.4x employee
        assert abs(employer_ei - employee_ei * 1.4) < 0.02

    def test_ei_above_max_insurable(self, tmp_db):
        """EI on $200,000 — capped at max insurable earnings."""
        out, rc = run_action(tmp_db, "compute-ei", gross_salary="200000", pay_period="annual")
        assert rc == 0
        ei = float(out["employee_ei"])
        max_ei = float(out["annual_max_employee"])
        assert ei == max_ei

    def test_ei_monthly(self, tmp_db):
        """EI on monthly payroll ($5,000/month)."""
        out, rc = run_action(tmp_db, "compute-ei", gross_salary="5000", pay_period="monthly")
        assert rc == 0
        assert float(out["employee_ei"]) > 0


class TestComputeFederalTax:
    def test_federal_below_bpa(self, tmp_db):
        """Federal tax on $15,000 — below BPA, $0 net tax."""
        out, rc = run_action(tmp_db, "compute-federal-tax", annual_income="15000")
        assert rc == 0
        assert float(out["net_tax"]) == 0

    def test_federal_first_bracket(self, tmp_db):
        """Federal tax on $40,000 — first bracket only (14% for 2026)."""
        out, rc = run_action(tmp_db, "compute-federal-tax", annual_income="40000")
        assert rc == 0
        assert float(out["net_tax"]) > 0
        assert float(out["marginal_rate"]) == 14

    def test_federal_high_income(self, tmp_db):
        """Federal tax on $300,000 — top bracket (33%)."""
        out, rc = run_action(tmp_db, "compute-federal-tax", annual_income="300000")
        assert rc == 0
        assert float(out["net_tax"]) > 0
        assert float(out["marginal_rate"]) == 33

    def test_federal_zero_income(self, tmp_db):
        """Federal tax on $0 income — $0 tax."""
        out, rc = run_action(tmp_db, "compute-federal-tax", annual_income="0")
        assert rc == 0
        assert float(out["net_tax"]) == 0


class TestComputeProvincialTax:
    def test_provincial_ontario(self, tmp_db):
        """Provincial tax for Ontario on $80,000."""
        out, rc = run_action(tmp_db, "compute-provincial-tax",
                             annual_income="80000", province="ON")
        assert rc == 0
        assert float(out["net_tax"]) > 0
        assert out["province"] == "ON"

    def test_provincial_bc(self, tmp_db):
        """Provincial tax for BC on $80,000."""
        out, rc = run_action(tmp_db, "compute-provincial-tax",
                             annual_income="80000", province="BC")
        assert rc == 0
        assert float(out["net_tax"]) > 0

    def test_provincial_quebec(self, tmp_db):
        """Provincial tax for Quebec on $80,000 — typically higher."""
        out, rc = run_action(tmp_db, "compute-provincial-tax",
                             annual_income="80000", province="QC")
        assert rc == 0
        assert float(out["net_tax"]) > 0

    def test_provincial_alberta(self, tmp_db):
        """Provincial tax for Alberta on $80,000."""
        out, rc = run_action(tmp_db, "compute-provincial-tax",
                             annual_income="80000", province="AB")
        assert rc == 0
        assert float(out["net_tax"]) > 0


class TestComputeTotalPayrollDeductions:
    def test_total_deductions_ontario(self, tmp_db):
        """All deductions for Ontario employee earning $60,000."""
        out, rc = run_action(tmp_db, "compute-total-payroll-deductions",
                             gross_salary="60000", province="ON", pay_period="annual")
        assert rc == 0
        assert float(out["cpp"]) > 0
        assert float(out["ei"]) > 0
        assert float(out["federal_tax"]) > 0
        assert float(out["provincial_tax"]) > 0
        assert float(out["total_deductions"]) > 0
        net = float(out["net_pay"])
        assert net < 60000
        assert net > 0

    def test_total_deductions_quebec(self, tmp_db):
        """Quebec uses QPP instead of CPP."""
        out, rc = run_action(tmp_db, "compute-total-payroll-deductions",
                             gross_salary="60000", province="QC", pay_period="annual")
        assert rc == 0
        # Quebec has QPP (higher rate)
        assert "qpp" in out or "cpp" in out


class TestSeedPayroll:
    def test_seed_payroll_components(self, ca_company):
        """Seed creates CPP/EI/tax salary components."""
        db_path, company_id = ca_company
        out, rc = run_action(db_path, "seed-ca-payroll", company_id=company_id)
        assert rc == 0
        assert out["components_created"] >= 6

    def test_seed_payroll_rejects_non_ca(self, us_company):
        """Seed payroll rejects non-Canadian company."""
        db_path, company_id = us_company
        out, rc = run_action(db_path, "seed-ca-payroll", company_id=company_id)
        assert rc == 1
