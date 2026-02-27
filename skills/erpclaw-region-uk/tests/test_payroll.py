"""Tests for UK payroll deductions (PAYE, NI, student loan, pension)."""
from conftest import run_action


class TestComputePAYE:
    def test_paye_below_personal_allowance(self, tmp_db):
        """PAYE on 10,000 -- below PA, 0 tax."""
        out, rc = run_action(tmp_db, "compute-paye", annual_income="10000")
        assert rc == 0
        assert float(out["net_tax"]) == 0

    def test_paye_basic_rate(self, tmp_db):
        """PAYE on 30,000 -- basic rate band."""
        out, rc = run_action(tmp_db, "compute-paye", annual_income="30000")
        assert rc == 0
        assert float(out["net_tax"]) > 0
        assert float(out["marginal_rate"]) == 20

    def test_paye_higher_rate(self, tmp_db):
        """PAYE on 80,000 -- higher rate band."""
        out, rc = run_action(tmp_db, "compute-paye", annual_income="80000")
        assert rc == 0
        assert float(out["net_tax"]) > 0
        assert float(out["marginal_rate"]) == 40

    def test_paye_additional_rate(self, tmp_db):
        """PAYE on 200,000 -- additional rate (45%)."""
        out, rc = run_action(tmp_db, "compute-paye", annual_income="200000")
        assert rc == 0
        assert float(out["net_tax"]) > 0
        assert float(out["marginal_rate"]) == 45

    def test_paye_pa_taper(self, tmp_db):
        """PAYE on 125,140 -- PA fully tapered away."""
        out, rc = run_action(tmp_db, "compute-paye", annual_income="125140")
        assert rc == 0
        assert float(out["personal_allowance"]) == 0

    def test_paye_scottish(self, tmp_db):
        """PAYE on 80,000 for Scotland -- uses Scottish bands."""
        out, rc = run_action(tmp_db, "compute-paye", annual_income="80000", region="SCO")
        assert rc == 0
        assert float(out["net_tax"]) > 0


class TestComputeNI:
    def test_ni_on_30k(self, tmp_db):
        """NI on 30,000 annual salary."""
        out, rc = run_action(tmp_db, "compute-ni", annual_income="30000")
        assert rc == 0
        assert float(out["employee_ni"]) > 0
        assert float(out["employer_ni"]) > 0

    def test_ni_below_primary_threshold(self, tmp_db):
        """NI on 10,000 -- below primary threshold, 0 employee NI."""
        out, rc = run_action(tmp_db, "compute-ni", annual_income="10000")
        assert rc == 0
        assert float(out["employee_ni"]) == 0

    def test_ni_above_uel(self, tmp_db):
        """NI on 80,000 -- above UEL, 2% on excess."""
        out, rc = run_action(tmp_db, "compute-ni", annual_income="80000")
        assert rc == 0
        assert float(out["employee_ni"]) > 0
        assert float(out["employer_ni"]) > 0


class TestComputeStudentLoan:
    def test_plan_1(self, tmp_db):
        """Student loan plan 1 on 30,000."""
        out, rc = run_action(tmp_db, "compute-student-loan", annual_income="30000", plan="1")
        assert rc == 0
        assert float(out["annual_deduction"]) > 0

    def test_below_threshold(self, tmp_db):
        """Student loan plan 2 on 20,000 -- below threshold, 0."""
        out, rc = run_action(tmp_db, "compute-student-loan", annual_income="20000", plan="2")
        assert rc == 0
        assert float(out["annual_deduction"]) == 0

    def test_postgraduate(self, tmp_db):
        """Postgraduate loan on 30,000."""
        out, rc = run_action(tmp_db, "compute-student-loan", annual_income="30000", plan="PG")
        assert rc == 0
        assert float(out["annual_deduction"]) > 0
        assert float(out["rate"]) == 6


class TestComputePension:
    def test_pension_on_30k(self, tmp_db):
        """NEST pension on 30,000."""
        out, rc = run_action(tmp_db, "compute-pension", annual_salary="30000")
        assert rc == 0
        assert float(out["employee_contribution"]) > 0
        assert float(out["employer_contribution"]) > 0
        assert float(out["total_contribution"]) > 0

    def test_pension_below_qualifying(self, tmp_db):
        """Pension on 5,000 -- below lower qualifying, 0."""
        out, rc = run_action(tmp_db, "compute-pension", annual_salary="5000")
        assert rc == 0
        assert float(out["employee_contribution"]) == 0


class TestSeedPayroll:
    def test_seed_payroll_components(self, uk_company):
        """Seed creates PAYE/NI/pension/student loan components."""
        db_path, company_id = uk_company
        out, rc = run_action(db_path, "seed-uk-payroll", company_id=company_id)
        assert rc == 0
        assert out["components_created"] >= 6

    def test_seed_payroll_rejects_non_uk(self, non_uk_company):
        """Seed payroll rejects non-UK company."""
        db_path, company_id = non_uk_company
        out, rc = run_action(db_path, "seed-uk-payroll", company_id=company_id)
        assert rc == 1
