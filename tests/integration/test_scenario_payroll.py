"""Integration test scenario: Full Payroll Business Cycle.

Tests the complete payroll lifecycle from employee creation through salary
structure setup, payroll run processing, FICA/tax deductions, GL posting,
and W-2 data generation. Exercises cross-skill interactions between
erpclaw-hr and erpclaw-payroll, and validates GL integrity end-to-end.

Scenario flow:
    1. Create company, FY, naming series, GL accounts, cost center
    2. Create department and designation (erpclaw-hr)
    3. Create employee with federal filing status (erpclaw-hr)
    4. Create salary components -- Basic, HRA (erpclaw-payroll)
    5. Create salary structure with components (erpclaw-payroll)
    6. Assign salary structure to employee (erpclaw-payroll)
    7. Configure FICA rates (erpclaw-payroll)
    8. Configure income tax slab (erpclaw-payroll)
    9. Create payroll run (erpclaw-payroll)
   10. Generate salary slips (erpclaw-payroll)
   11. Verify deduction calculations (FICA, federal tax)
   12. Submit payroll run -- GL posting (erpclaw-payroll)
   13. Verify GL entries balanced (DR expense = CR liabilities)
   14. Generate W-2 data (erpclaw-payroll)
   15. Cancel payroll run and verify GL reversal
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_account,
    create_test_cost_center,
)


class TestPayrollScenario:
    """Full payroll cycle integration tests.

    Each test uses the fresh_db fixture (provided by conftest.py) which gives
    a clean database with all 173 tables created but no data.
    """

    # ------------------------------------------------------------------
    # Shared setup helper
    # ------------------------------------------------------------------

    @staticmethod
    def _setup_payroll_environment(conn):
        """Create company, FY, naming series, GL accounts, and cost center.

        Returns a dict with all IDs needed for payroll tests:
            company_id, fy_id, cost_center_id,
            salary_expense_id, payroll_payable_id, federal_tax_payable_id,
            ss_payable_id, medicare_payable_id, employer_tax_expense_id,
            bank_id
        """
        cid = create_test_company(conn, name="Payroll Corp", abbr="PC")
        fy_id = create_test_fiscal_year(conn, cid)
        seed_naming_series(conn, cid)
        cc = create_test_cost_center(conn, cid, name="Main")

        # Salary Expense (the _find_payroll_accounts function searches by name)
        salary_expense = create_test_account(
            conn, cid, "Salary Expense", "expense",
            account_type="expense", account_number="6100",
        )

        # Employer Payroll Tax Expense
        employer_tax_expense = create_test_account(
            conn, cid, "Employer Tax Expense", "expense",
            account_type="expense", account_number="6200",
        )

        # Payroll Payable (liability)
        payroll_payable = create_test_account(
            conn, cid, "Payroll Payable", "liability",
            account_type="payable", account_number="2300",
        )

        # Federal Income Tax Withheld (liability)
        federal_tax_payable = create_test_account(
            conn, cid, "Federal Income Tax Withheld", "liability",
            account_type="tax", account_number="2310",
        )

        # Social Security Payable (liability)
        ss_payable = create_test_account(
            conn, cid, "Social Security Payable", "liability",
            account_type="tax", account_number="2320",
        )

        # Medicare Payable (liability)
        medicare_payable = create_test_account(
            conn, cid, "Medicare Payable", "liability",
            account_type="tax", account_number="2330",
        )

        # Bank / Cash (asset)
        bank = create_test_account(
            conn, cid, "Bank", "asset",
            account_type="bank", account_number="1010",
        )

        return {
            "company_id": cid,
            "fy_id": fy_id,
            "cost_center_id": cc,
            "salary_expense_id": salary_expense,
            "employer_tax_expense_id": employer_tax_expense,
            "payroll_payable_id": payroll_payable,
            "federal_tax_payable_id": federal_tax_payable,
            "ss_payable_id": ss_payable,
            "medicare_payable_id": medicare_payable,
            "bank_id": bank,
        }

    @staticmethod
    def _create_department(conn, company_id, name="Engineering"):
        """Create a department via erpclaw-hr. Returns department_id."""
        result = _call_action(
            "erpclaw-hr", "add-department", conn,
            name=name, company_id=company_id,
        )
        assert result["status"] == "ok", f"add-department failed: {result}"
        return result["department_id"]

    @staticmethod
    def _create_designation(conn, name="Software Engineer"):
        """Create a designation via erpclaw-hr. Returns designation_id."""
        result = _call_action(
            "erpclaw-hr", "add-designation", conn,
            name=name,
        )
        assert result["status"] == "ok", f"add-designation failed: {result}"
        return result["designation_id"]

    @staticmethod
    def _create_employee(conn, company_id, department_id, designation_id,
                         first_name="John", last_name="Doe",
                         filing_status="single", w4_allowances="0"):
        """Create an employee via erpclaw-hr. Returns employee_id."""
        result = _call_action(
            "erpclaw-hr", "add-employee", conn,
            first_name=first_name,
            last_name=last_name,
            date_of_birth="1990-05-15",
            gender="male",
            date_of_joining="2026-01-01",
            employment_type="full_time",
            company_id=company_id,
            department_id=department_id,
            designation_id=designation_id,
            federal_filing_status=filing_status,
            w4_allowances=w4_allowances,
        )
        assert result["status"] == "ok", f"add-employee failed: {result}"
        return result["employee_id"]

    @staticmethod
    def _create_salary_components(conn):
        """Create Basic and HRA salary components. Returns (basic_id, hra_id)."""
        r1 = _call_action(
            "erpclaw-payroll", "add-salary-component", conn,
            name="Basic Salary",
            component_type="earning",
            is_tax_applicable="1",
        )
        assert r1["status"] == "ok", f"add-salary-component Basic failed: {r1}"

        r2 = _call_action(
            "erpclaw-payroll", "add-salary-component", conn,
            name="House Rent Allowance",
            component_type="earning",
            is_tax_applicable="1",
        )
        assert r2["status"] == "ok", f"add-salary-component HRA failed: {r2}"

        return r1["salary_component_id"], r2["salary_component_id"]

    @staticmethod
    def _create_salary_structure(conn, company_id, basic_id, hra_id,
                                  name="Standard Structure"):
        """Create a salary structure with Basic (fixed amount) + HRA (40% of Basic).

        Returns salary_structure_id.
        """
        components = json.dumps([
            {
                "salary_component_id": basic_id,
                "amount": "0",
                "sort_order": 0,
            },
            {
                "salary_component_id": hra_id,
                "percentage": "40",
                "base_component_id": basic_id,
                "sort_order": 1,
            },
        ])
        result = _call_action(
            "erpclaw-payroll", "add-salary-structure", conn,
            name=name,
            payroll_frequency="monthly",
            company_id=company_id,
            components=components,
        )
        assert result["status"] == "ok", f"add-salary-structure failed: {result}"
        return result["salary_structure_id"]

    @staticmethod
    def _assign_salary(conn, employee_id, structure_id, base_amount="5000.00"):
        """Assign salary structure to employee. Returns assignment_id."""
        result = _call_action(
            "erpclaw-payroll", "add-salary-assignment", conn,
            employee_id=employee_id,
            salary_structure_id=structure_id,
            base_amount=base_amount,
            effective_from="2026-01-01",
        )
        assert result["status"] == "ok", f"add-salary-assignment failed: {result}"
        return result["salary_assignment_id"]

    @staticmethod
    def _configure_fica(conn):
        """Configure 2026 FICA rates. Returns the result dict."""
        result = _call_action(
            "erpclaw-payroll", "update-fica-config", conn,
            tax_year="2026",
            ss_wage_base="168600",
            ss_employee_rate="6.2",
            ss_employer_rate="6.2",
            medicare_employee_rate="1.45",
            medicare_employer_rate="1.45",
            additional_medicare_threshold="200000",
            additional_medicare_rate="0.9",
        )
        assert result["status"] == "ok", f"update-fica-config failed: {result}"
        return result

    @staticmethod
    def _configure_income_tax(conn):
        """Configure 2026 federal income tax slab. Returns result dict."""
        rates = json.dumps([
            {"from_amount": "0", "to_amount": "11600", "rate": "10"},
            {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
            {"from_amount": "47150", "to_amount": "100525", "rate": "22"},
            {"from_amount": "100525", "to_amount": "191950", "rate": "24"},
            {"from_amount": "191950", "to_amount": "243725", "rate": "32"},
            {"from_amount": "243725", "to_amount": "609350", "rate": "35"},
            {"from_amount": "609350", "to_amount": None, "rate": "37"},
        ])
        result = _call_action(
            "erpclaw-payroll", "add-income-tax-slab", conn,
            name="Federal 2026 Single",
            tax_jurisdiction="federal",
            filing_status="single",
            effective_from="2026-01-01",
            standard_deduction="14600",
            rates=rates,
        )
        assert result["status"] == "ok", f"add-income-tax-slab failed: {result}"
        return result

    @staticmethod
    def _full_payroll_setup(conn):
        """Run all setup steps and return a dict with every ID needed.

        Returns:
            dict with keys: company_id, fy_id, cost_center_id, department_id,
            designation_id, employee_id, basic_id, hra_id, structure_id,
            assignment_id, and all account IDs from _setup_payroll_environment.
        """
        env = TestPayrollScenario._setup_payroll_environment(conn)
        dept_id = TestPayrollScenario._create_department(conn, env["company_id"])
        desig_id = TestPayrollScenario._create_designation(conn)
        emp_id = TestPayrollScenario._create_employee(
            conn, env["company_id"], dept_id, desig_id,
        )
        basic_id, hra_id = TestPayrollScenario._create_salary_components(conn)
        struct_id = TestPayrollScenario._create_salary_structure(
            conn, env["company_id"], basic_id, hra_id,
        )
        assign_id = TestPayrollScenario._assign_salary(conn, emp_id, struct_id)
        TestPayrollScenario._configure_fica(conn)
        TestPayrollScenario._configure_income_tax(conn)

        env.update({
            "department_id": dept_id,
            "designation_id": desig_id,
            "employee_id": emp_id,
            "basic_id": basic_id,
            "hra_id": hra_id,
            "structure_id": struct_id,
            "assignment_id": assign_id,
        })
        return env

    # ------------------------------------------------------------------
    # 1. test_full_payroll_cycle
    # ------------------------------------------------------------------

    def test_full_payroll_cycle(self, fresh_db):
        """End-to-end: employee setup, payroll config, run, submit, verify GL.

        Creates all payroll infrastructure from scratch, runs a single
        monthly payroll, submits it, and verifies that GL entries are
        balanced and correctly categorized.
        """
        conn = fresh_db
        env = self._full_payroll_setup(conn)

        # Create payroll run
        r = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        assert r["status"] == "ok"
        run_id = r["payroll_run_id"]

        # Generate salary slips
        r = _call_action(
            "erpclaw-payroll", "generate-salary-slips", conn,
            payroll_run_id=run_id,
        )
        assert r["status"] == "ok"
        assert r["slips_generated"] == 1
        assert Decimal(r["total_gross"]) > Decimal("0")

        # Submit payroll run (posts GL)
        r = _call_action(
            "erpclaw-payroll", "submit-payroll-run", conn,
            payroll_run_id=run_id,
            cost_center_id=env["cost_center_id"],
        )
        assert r["status"] == "ok"
        assert r["gl_entries"] >= 2

        # Verify GL balance: SUM(debit) == SUM(credit) on non-cancelled entries
        gl_rows = conn.execute(
            "SELECT * FROM gl_entry WHERE is_cancelled = 0"
        ).fetchall()
        total_debit = sum(Decimal(g["debit"]) for g in gl_rows)
        total_credit = sum(Decimal(g["credit"]) for g in gl_rows)
        assert abs(total_debit - total_credit) < Decimal("0.02"), (
            f"GL not balanced: debit={total_debit}, credit={total_credit}"
        )

        # Verify payroll run status
        run_row = conn.execute(
            "SELECT status FROM payroll_run WHERE id = ?", (run_id,)
        ).fetchone()
        assert run_row["status"] == "submitted"

    # ------------------------------------------------------------------
    # 2. test_employee_creation
    # ------------------------------------------------------------------

    def test_employee_creation(self, fresh_db):
        """Create department, designation, and employee with full details.

        Verifies the employee record contains correct department, designation,
        filing status, and is in 'active' status.
        """
        conn = fresh_db
        env = self._setup_payroll_environment(conn)

        dept_id = self._create_department(conn, env["company_id"], "Finance")
        desig_id = self._create_designation(conn, "Accountant")

        emp_id = self._create_employee(
            conn, env["company_id"], dept_id, desig_id,
            first_name="Jane", last_name="Smith",
            filing_status="married_jointly", w4_allowances="2",
        )

        # Verify employee in DB
        emp = conn.execute(
            "SELECT * FROM employee WHERE id = ?", (emp_id,)
        ).fetchone()
        assert emp is not None
        assert emp["first_name"] == "Jane"
        assert emp["last_name"] == "Smith"
        assert emp["full_name"] == "Jane Smith"
        assert emp["department_id"] == dept_id
        assert emp["designation_id"] == desig_id
        assert emp["federal_filing_status"] == "married_jointly"
        assert emp["w4_allowances"] == 2
        assert emp["status"] == "active"
        assert emp["employment_type"] == "full_time"
        assert emp["company_id"] == env["company_id"]

    # ------------------------------------------------------------------
    # 3. test_salary_components_setup
    # ------------------------------------------------------------------

    def test_salary_components_setup(self, fresh_db):
        """Create earning and deduction salary components.

        Verifies components are stored with correct types and default flags.
        """
        conn = fresh_db

        # Create earning component
        r1 = _call_action(
            "erpclaw-payroll", "add-salary-component", conn,
            name="Base Pay",
            component_type="earning",
            is_tax_applicable="1",
        )
        assert r1["status"] == "ok"
        assert r1["component_type"] == "earning"

        # Create deduction component
        r2 = _call_action(
            "erpclaw-payroll", "add-salary-component", conn,
            name="Health Insurance",
            component_type="deduction",
            is_tax_applicable="0",
            is_pre_tax="1",
        )
        assert r2["status"] == "ok"
        assert r2["component_type"] == "deduction"

        # Verify in DB
        comp1 = conn.execute(
            "SELECT * FROM salary_component WHERE id = ?",
            (r1["salary_component_id"],),
        ).fetchone()
        assert comp1["is_tax_applicable"] == 1
        assert comp1["component_type"] == "earning"

        comp2 = conn.execute(
            "SELECT * FROM salary_component WHERE id = ?",
            (r2["salary_component_id"],),
        ).fetchone()
        assert comp2["is_tax_applicable"] == 0
        assert comp2["is_pre_tax"] == 1
        assert comp2["component_type"] == "deduction"

    # ------------------------------------------------------------------
    # 4. test_salary_structure
    # ------------------------------------------------------------------

    def test_salary_structure(self, fresh_db):
        """Create salary structure with Basic + HRA components.

        Verifies structure record and detail rows are created correctly,
        with HRA calculated as 40% of Basic.
        """
        conn = fresh_db
        env = self._setup_payroll_environment(conn)
        basic_id, hra_id = self._create_salary_components(conn)

        struct_id = self._create_salary_structure(
            conn, env["company_id"], basic_id, hra_id,
        )

        # Verify structure record
        ss = conn.execute(
            "SELECT * FROM salary_structure WHERE id = ?", (struct_id,)
        ).fetchone()
        assert ss is not None
        assert ss["name"] == "Standard Structure"
        assert ss["payroll_frequency"] == "monthly"
        assert ss["company_id"] == env["company_id"]
        assert ss["is_active"] == 1

        # Verify structure details
        details = conn.execute(
            """SELECT ssd.*, sc.name AS component_name
               FROM salary_structure_detail ssd
               JOIN salary_component sc ON sc.id = ssd.salary_component_id
               WHERE ssd.salary_structure_id = ?
               ORDER BY ssd.sort_order ASC""",
            (struct_id,),
        ).fetchall()
        assert len(details) == 2

        # First detail: Basic Salary with amount "0" (uses base_amount from assignment)
        assert details[0]["component_name"] == "Basic Salary"
        assert details[0]["amount"] == "0"

        # Second detail: HRA with 40% of Basic
        assert details[1]["component_name"] == "House Rent Allowance"
        assert Decimal(details[1]["percentage"]) == Decimal("40")
        assert details[1]["base_component_id"] == basic_id

    # ------------------------------------------------------------------
    # 5. test_salary_assignment
    # ------------------------------------------------------------------

    def test_salary_assignment(self, fresh_db):
        """Assign salary structure to employee with base amount.

        Verifies the assignment links correctly and stores the proper
        base amount and effective dates.
        """
        conn = fresh_db
        env = self._setup_payroll_environment(conn)
        dept_id = self._create_department(conn, env["company_id"])
        desig_id = self._create_designation(conn)
        emp_id = self._create_employee(
            conn, env["company_id"], dept_id, desig_id,
        )
        basic_id, hra_id = self._create_salary_components(conn)
        struct_id = self._create_salary_structure(
            conn, env["company_id"], basic_id, hra_id,
        )

        result = _call_action(
            "erpclaw-payroll", "add-salary-assignment", conn,
            employee_id=emp_id,
            salary_structure_id=struct_id,
            base_amount="6000.00",
            effective_from="2026-02-01",
        )
        assert result["status"] == "ok"
        assert result["employee_id"] == emp_id
        assert result["salary_structure_id"] == struct_id
        assert result["base_amount"] == "6000.00"
        assert result["effective_from"] == "2026-02-01"

        # Verify in DB
        sa = conn.execute(
            "SELECT * FROM salary_assignment WHERE id = ?",
            (result["salary_assignment_id"],),
        ).fetchone()
        assert sa is not None
        assert sa["employee_id"] == emp_id
        assert sa["salary_structure_id"] == struct_id
        assert Decimal(sa["base_amount"]) == Decimal("6000.00")
        assert sa["effective_from"] == "2026-02-01"
        assert sa["effective_to"] is None
        assert sa["company_id"] == env["company_id"]

    # ------------------------------------------------------------------
    # 6. test_fica_configuration
    # ------------------------------------------------------------------

    def test_fica_configuration(self, fresh_db):
        """Configure FICA rates for tax year 2026.

        Verifies the fica_config table is populated with correct rates
        for Social Security and Medicare.
        """
        conn = fresh_db

        result = self._configure_fica(conn)
        assert result["tax_year"] == 2026
        assert result["ss_wage_base"] == "168600.00"
        assert result["ss_employee_rate"] == "6.20"
        assert result["ss_employer_rate"] == "6.20"
        assert result["medicare_employee_rate"] == "1.45"
        assert result["medicare_employer_rate"] == "1.45"
        assert result["additional_medicare_threshold"] == "200000.00"
        assert result["additional_medicare_rate"] == "0.90"

        # Verify in DB
        fica = conn.execute(
            "SELECT * FROM fica_config WHERE tax_year = 2026"
        ).fetchone()
        assert fica is not None
        assert Decimal(fica["ss_wage_base"]) == Decimal("168600.00")
        assert Decimal(fica["ss_employee_rate"]) == Decimal("6.20")
        assert Decimal(fica["medicare_employee_rate"]) == Decimal("1.45")

    # ------------------------------------------------------------------
    # 7. test_income_tax_slab
    # ------------------------------------------------------------------

    def test_income_tax_slab(self, fresh_db):
        """Configure federal income tax brackets for 2026.

        Verifies the income_tax_slab and rate entries are stored correctly
        with contiguous brackets.
        """
        conn = fresh_db

        result = self._configure_income_tax(conn)
        slab_id = result["income_tax_slab_id"]
        assert result["tax_jurisdiction"] == "federal"
        assert result["filing_status"] == "single"
        assert result["standard_deduction"] == "14600.00"
        assert result["rate_count"] == 7

        # Verify slab record
        slab = conn.execute(
            "SELECT * FROM income_tax_slab WHERE id = ?", (slab_id,)
        ).fetchone()
        assert slab is not None
        assert slab["is_active"] == 1
        assert slab["effective_from"] == "2026-01-01"

        # Verify rate brackets are contiguous
        rates = conn.execute(
            """SELECT from_amount, to_amount, rate
               FROM income_tax_slab_rate
               WHERE slab_id = ?
               ORDER BY CAST(from_amount AS REAL) ASC""",
            (slab_id,),
        ).fetchall()
        assert len(rates) == 7

        # First bracket starts at 0
        assert Decimal(rates[0]["from_amount"]) == Decimal("0")
        # Last bracket has no upper bound
        assert rates[6]["to_amount"] is None
        # Check contiguity of middle brackets
        for i in range(1, len(rates)):
            prev_to = rates[i - 1]["to_amount"]
            curr_from = rates[i]["from_amount"]
            if prev_to is not None:
                assert Decimal(curr_from) == Decimal(prev_to), (
                    f"Bracket {i} not contiguous: prev.to={prev_to}, curr.from={curr_from}"
                )

    # ------------------------------------------------------------------
    # 8. test_create_payroll_run
    # ------------------------------------------------------------------

    def test_create_payroll_run(self, fresh_db):
        """Create a draft payroll run for January 2026.

        Verifies the payroll_run record is created with correct period,
        draft status, and zero totals.
        """
        conn = fresh_db
        env = self._setup_payroll_environment(conn)

        result = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        assert result["status"] == "ok"
        run_id = result["payroll_run_id"]
        assert result["period_start"] == "2026-01-01"
        assert result["period_end"] == "2026-01-31"
        assert result["payroll_frequency"] == "monthly"
        assert result.get("naming_series") is not None

        # Verify in DB
        run = conn.execute(
            "SELECT * FROM payroll_run WHERE id = ?", (run_id,)
        ).fetchone()
        assert run is not None
        assert run["status"] == "draft"
        assert run["company_id"] == env["company_id"]
        assert Decimal(run["total_gross"]) == Decimal("0")
        assert Decimal(run["total_net"]) == Decimal("0")
        assert run["employee_count"] == 0

    # ------------------------------------------------------------------
    # 9. test_generate_salary_slips
    # ------------------------------------------------------------------

    def test_generate_salary_slips(self, fresh_db):
        """Generate salary slips for a payroll run with one employee.

        Verifies that the slip is created with correct gross, deductions,
        and net pay amounts. With base_amount=5000 and structure
        Basic(base) + HRA(40% of Basic):
            Basic = 5000, HRA = 2000, Gross = 7000
        """
        conn = fresh_db
        env = self._full_payroll_setup(conn)

        r = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        run_id = r["payroll_run_id"]

        r = _call_action(
            "erpclaw-payroll", "generate-salary-slips", conn,
            payroll_run_id=run_id,
        )
        assert r["status"] == "ok"
        assert r["slips_generated"] == 1

        # Gross should be 5000 (Basic) + 2000 (HRA 40%) = 7000
        total_gross = Decimal(r["total_gross"])
        assert total_gross == Decimal("7000.00"), (
            f"Expected gross=7000.00, got {total_gross}"
        )

        # Verify salary slip record
        slip = conn.execute(
            "SELECT * FROM salary_slip WHERE payroll_run_id = ?",
            (run_id,),
        ).fetchone()
        assert slip is not None
        assert slip["employee_id"] == env["employee_id"]
        assert Decimal(slip["gross_pay"]) == Decimal("7000.00")
        assert slip["status"] == "draft"

        # Net = gross - deductions; deductions > 0
        assert Decimal(slip["total_deductions"]) > Decimal("0")
        assert Decimal(slip["net_pay"]) > Decimal("0")
        assert Decimal(slip["net_pay"]) < total_gross

        # Verify slip details (earnings)
        earnings = conn.execute(
            """SELECT ssd.*, sc.name AS component_name
               FROM salary_slip_detail ssd
               JOIN salary_component sc ON sc.id = ssd.salary_component_id
               WHERE ssd.salary_slip_id = ? AND ssd.component_type = 'earning'
               ORDER BY sc.name""",
            (slip["id"],),
        ).fetchall()
        assert len(earnings) == 2

        # Map component names to amounts
        earning_map = {e["component_name"]: Decimal(e["amount"]) for e in earnings}
        assert earning_map["Basic Salary"] == Decimal("5000.00")
        assert earning_map["House Rent Allowance"] == Decimal("2000.00")

    # ------------------------------------------------------------------
    # 10. test_salary_slip_deductions
    # ------------------------------------------------------------------

    def test_salary_slip_deductions(self, fresh_db):
        """Verify FICA and federal income tax deductions on salary slip.

        With gross = 7000/month (annual = 84000):
        - SS: 7000 * 6.2% = 434.00
        - Medicare: 7000 * 1.45% = 101.50
        - Federal tax: computed via progressive brackets after annualizing
        """
        conn = fresh_db
        env = self._full_payroll_setup(conn)

        r = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        run_id = r["payroll_run_id"]

        _call_action(
            "erpclaw-payroll", "generate-salary-slips", conn,
            payroll_run_id=run_id,
        )

        slip = conn.execute(
            "SELECT * FROM salary_slip WHERE payroll_run_id = ?",
            (run_id,),
        ).fetchone()

        # Get all deductions
        deductions = conn.execute(
            """SELECT ssd.*, sc.name AS component_name
               FROM salary_slip_detail ssd
               JOIN salary_component sc ON sc.id = ssd.salary_component_id
               WHERE ssd.salary_slip_id = ? AND ssd.component_type = 'deduction'""",
            (slip["id"],),
        ).fetchall()

        deduction_map = {d["component_name"]: Decimal(d["amount"]) for d in deductions}

        # Social Security: 7000 * 6.2% = 434.00
        assert "Social Security Tax" in deduction_map
        assert deduction_map["Social Security Tax"] == Decimal("434.00"), (
            f"Expected SS=434.00, got {deduction_map['Social Security Tax']}"
        )

        # Medicare: 7000 * 1.45% = 101.50
        assert "Medicare Tax" in deduction_map
        assert deduction_map["Medicare Tax"] == Decimal("101.50"), (
            f"Expected Medicare=101.50, got {deduction_map['Medicare Tax']}"
        )

        # Federal income tax should be > 0
        assert "Federal Income Tax" in deduction_map
        assert deduction_map["Federal Income Tax"] > Decimal("0"), (
            "Federal income tax should be positive"
        )

        # Verify gross - total_deductions == net
        gross = Decimal(slip["gross_pay"])
        total_ded = Decimal(slip["total_deductions"])
        net = Decimal(slip["net_pay"])
        assert abs((gross - total_ded) - net) < Decimal("0.02"), (
            f"gross({gross}) - deductions({total_ded}) != net({net})"
        )

    # ------------------------------------------------------------------
    # 11. test_submit_payroll_run
    # ------------------------------------------------------------------

    def test_submit_payroll_run(self, fresh_db):
        """Submit a payroll run and verify status transitions.

        After submission, the payroll run and all salary slips should
        be in 'submitted' status, and GL entries should be created.
        """
        conn = fresh_db
        env = self._full_payroll_setup(conn)

        r = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        run_id = r["payroll_run_id"]

        _call_action(
            "erpclaw-payroll", "generate-salary-slips", conn,
            payroll_run_id=run_id,
        )

        r = _call_action(
            "erpclaw-payroll", "submit-payroll-run", conn,
            payroll_run_id=run_id,
            cost_center_id=env["cost_center_id"],
        )
        assert r["status"] == "ok"
        assert r["gl_entries"] >= 2

        # Payroll run should be submitted
        run_row = conn.execute(
            "SELECT status FROM payroll_run WHERE id = ?", (run_id,)
        ).fetchone()
        assert run_row["status"] == "submitted"

        # All salary slips should be submitted
        slip_statuses = conn.execute(
            "SELECT status FROM salary_slip WHERE payroll_run_id = ?",
            (run_id,),
        ).fetchall()
        for s in slip_statuses:
            assert s["status"] == "submitted"

        # GL entries exist for this payroll
        gl_count = conn.execute(
            """SELECT COUNT(*) AS cnt FROM gl_entry
               WHERE voucher_type = 'payroll_entry' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (run_id,),
        ).fetchone()["cnt"]
        assert gl_count >= 2

    # ------------------------------------------------------------------
    # 12. test_payroll_gl_entries
    # ------------------------------------------------------------------

    def test_payroll_gl_entries(self, fresh_db):
        """Verify GL entry structure: salary expense DR, tax/payable CR.

        After submitting payroll, GL should contain:
        - At least one DR to a salary expense account
        - At least one CR to payroll payable (net pay)
        - CR entries for tax withholdings (federal, SS, Medicare)
        - Overall DR == CR (balanced)
        """
        conn = fresh_db
        env = self._full_payroll_setup(conn)

        r = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        run_id = r["payroll_run_id"]

        _call_action(
            "erpclaw-payroll", "generate-salary-slips", conn,
            payroll_run_id=run_id,
        )

        r = _call_action(
            "erpclaw-payroll", "submit-payroll-run", conn,
            payroll_run_id=run_id,
            cost_center_id=env["cost_center_id"],
        )

        gl_rows = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'payroll_entry' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (run_id,),
        ).fetchall()
        assert len(gl_rows) >= 2

        # Categorize entries
        debit_entries = [g for g in gl_rows if Decimal(g["debit"]) > 0]
        credit_entries = [g for g in gl_rows if Decimal(g["credit"]) > 0]

        assert len(debit_entries) >= 1, "Should have at least one DR entry (salary expense)"
        assert len(credit_entries) >= 1, "Should have at least one CR entry (payroll payable)"

        # Check that salary expense account is debited
        salary_expense_debits = [
            g for g in debit_entries
            if g["account_id"] == env["salary_expense_id"]
        ]
        assert len(salary_expense_debits) >= 1, (
            "Expected debit to salary expense account"
        )

        # Check that payroll payable is credited
        payroll_payable_credits = [
            g for g in credit_entries
            if g["account_id"] == env["payroll_payable_id"]
        ]
        assert len(payroll_payable_credits) >= 1, (
            "Expected credit to payroll payable account"
        )

        # Balanced
        total_debit = sum(Decimal(g["debit"]) for g in gl_rows)
        total_credit = sum(Decimal(g["credit"]) for g in gl_rows)
        assert abs(total_debit - total_credit) < Decimal("0.02"), (
            f"GL not balanced: DR={total_debit}, CR={total_credit}"
        )

    # ------------------------------------------------------------------
    # 13. test_w2_data_generation
    # ------------------------------------------------------------------

    def test_w2_data_generation(self, fresh_db):
        """Generate W-2 data after a submitted payroll and verify totals.

        W-2 boxes should contain:
        - Box 1: Wages (gross minus pre-tax deductions)
        - Box 2: Federal income tax withheld
        - Box 3: Social Security wages (capped)
        - Box 4: Social Security tax withheld
        - Box 5: Medicare wages (= gross)
        - Box 6: Medicare tax withheld
        """
        conn = fresh_db
        env = self._full_payroll_setup(conn)

        # Run and submit payroll for January
        r = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        run_id = r["payroll_run_id"]

        _call_action(
            "erpclaw-payroll", "generate-salary-slips", conn,
            payroll_run_id=run_id,
        )

        _call_action(
            "erpclaw-payroll", "submit-payroll-run", conn,
            payroll_run_id=run_id,
            cost_center_id=env["cost_center_id"],
        )

        # Generate W-2 data
        r = _call_action(
            "erpclaw-payroll", "generate-w2-data", conn,
            tax_year="2026",
            company_id=env["company_id"],
        )
        assert r["status"] == "ok"
        assert r["tax_year"] == 2026
        assert r["employee_count"] == 1

        w2 = r["w2_data"][0]
        assert w2["employee_id"] == env["employee_id"]
        boxes = w2["boxes"]

        # Box 1: wages (gross = 7000 with no pre-tax deductions like 401k)
        box1 = Decimal(boxes["1"])
        assert box1 == Decimal("7000.00"), f"Box 1 should be 7000.00, got {box1}"

        # Box 2: federal income tax > 0
        box2 = Decimal(boxes["2"])
        assert box2 > Decimal("0"), "Box 2 (federal tax) should be positive"

        # Box 3: SS wages = min(gross, 168600) = 7000
        box3 = Decimal(boxes["3"])
        assert box3 == Decimal("7000.00"), f"Box 3 should be 7000.00, got {box3}"

        # Box 4: SS tax = 434.00
        box4 = Decimal(boxes["4"])
        assert box4 == Decimal("434.00"), f"Box 4 should be 434.00, got {box4}"

        # Box 5: Medicare wages = gross = 7000
        box5 = Decimal(boxes["5"])
        assert box5 == Decimal("7000.00"), f"Box 5 should be 7000.00, got {box5}"

        # Box 6: Medicare tax = 101.50
        box6 = Decimal(boxes["6"])
        assert box6 == Decimal("101.50"), f"Box 6 should be 101.50, got {box6}"

    # ------------------------------------------------------------------
    # 14. test_multiple_employees
    # ------------------------------------------------------------------

    def test_multiple_employees(self, fresh_db):
        """Run payroll with 3 employees at different salary levels.

        Verifies each employee gets their own salary slip and the payroll
        run totals aggregate correctly.
        """
        conn = fresh_db
        env = self._setup_payroll_environment(conn)
        dept_id = self._create_department(conn, env["company_id"])
        desig_id = self._create_designation(conn)
        basic_id, hra_id = self._create_salary_components(conn)
        struct_id = self._create_salary_structure(
            conn, env["company_id"], basic_id, hra_id,
        )
        self._configure_fica(conn)
        self._configure_income_tax(conn)

        # Create 3 employees with different salaries
        employees = []
        salaries = [("Alice", "Wang", "4000.00"), ("Bob", "Jones", "6000.00"),
                     ("Carol", "Davis", "8000.00")]

        for first, last, base in salaries:
            emp_id = self._create_employee(
                conn, env["company_id"], dept_id, desig_id,
                first_name=first, last_name=last,
            )
            self._assign_salary(conn, emp_id, struct_id, base_amount=base)
            employees.append(emp_id)

        # Create and generate payroll
        r = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        run_id = r["payroll_run_id"]

        r = _call_action(
            "erpclaw-payroll", "generate-salary-slips", conn,
            payroll_run_id=run_id,
        )
        assert r["status"] == "ok"
        assert r["slips_generated"] == 3

        # Verify each employee has a salary slip
        for emp_id in employees:
            slip = conn.execute(
                "SELECT * FROM salary_slip WHERE payroll_run_id = ? AND employee_id = ?",
                (run_id, emp_id),
            ).fetchone()
            assert slip is not None, f"No salary slip for employee {emp_id}"
            assert Decimal(slip["gross_pay"]) > Decimal("0")

        # Verify run totals match sum of slip totals
        slips = conn.execute(
            "SELECT * FROM salary_slip WHERE payroll_run_id = ?",
            (run_id,),
        ).fetchall()
        sum_gross = sum(Decimal(s["gross_pay"]) for s in slips)
        sum_net = sum(Decimal(s["net_pay"]) for s in slips)
        sum_ded = sum(Decimal(s["total_deductions"]) for s in slips)

        assert Decimal(r["total_gross"]) == sum_gross
        assert Decimal(r["total_net"]) == sum_net
        assert Decimal(r["total_deductions"]) == sum_ded

        # Verify individual gross calculations:
        # Alice: 4000 + 40%*4000 = 4000 + 1600 = 5600
        # Bob: 6000 + 40%*6000 = 6000 + 2400 = 8400
        # Carol: 8000 + 40%*8000 = 8000 + 3200 = 11200
        expected_total_gross = Decimal("5600.00") + Decimal("8400.00") + Decimal("11200.00")
        assert sum_gross == expected_total_gross, (
            f"Expected total gross={expected_total_gross}, got {sum_gross}"
        )

        # Submit and verify GL
        r = _call_action(
            "erpclaw-payroll", "submit-payroll-run", conn,
            payroll_run_id=run_id,
            cost_center_id=env["cost_center_id"],
        )
        assert r["status"] == "ok"
        assert r["gl_entries"] >= 2

        # GL balanced
        gl_rows = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'payroll_entry' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (run_id,),
        ).fetchall()
        total_dr = sum(Decimal(g["debit"]) for g in gl_rows)
        total_cr = sum(Decimal(g["credit"]) for g in gl_rows)
        assert abs(total_dr - total_cr) < Decimal("0.02"), (
            f"Multi-employee GL not balanced: DR={total_dr}, CR={total_cr}"
        )

    # ------------------------------------------------------------------
    # 15. test_payroll_cancellation
    # ------------------------------------------------------------------

    def test_payroll_cancellation(self, fresh_db):
        """Cancel a submitted payroll run and verify GL reversal.

        After cancellation:
        - Payroll run status should be 'cancelled'
        - All salary slips should be 'cancelled'
        - Original GL entries should be marked cancelled
        - Reversal GL entries should be created
        - Net GL should be zero
        """
        conn = fresh_db
        env = self._full_payroll_setup(conn)

        # Create, generate, submit
        r = _call_action(
            "erpclaw-payroll", "create-payroll-run", conn,
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            payroll_frequency="monthly",
        )
        run_id = r["payroll_run_id"]

        _call_action(
            "erpclaw-payroll", "generate-salary-slips", conn,
            payroll_run_id=run_id,
        )

        submit_result = _call_action(
            "erpclaw-payroll", "submit-payroll-run", conn,
            payroll_run_id=run_id,
            cost_center_id=env["cost_center_id"],
        )
        original_gl_count = submit_result["gl_entries"]
        assert original_gl_count >= 2

        # Cancel
        r = _call_action(
            "erpclaw-payroll", "cancel-payroll-run", conn,
            payroll_run_id=run_id,
        )
        assert r["status"] == "ok"
        assert r["reversed_entries"] >= 2

        # Payroll run should be cancelled
        run_row = conn.execute(
            "SELECT status FROM payroll_run WHERE id = ?", (run_id,)
        ).fetchone()
        assert run_row["status"] == "cancelled"

        # All salary slips should be cancelled
        slips = conn.execute(
            "SELECT status FROM salary_slip WHERE payroll_run_id = ?",
            (run_id,),
        ).fetchall()
        for s in slips:
            assert s["status"] == "cancelled"

        # Original GL entries should be marked as cancelled
        cancelled_gl = conn.execute(
            """SELECT COUNT(*) AS cnt FROM gl_entry
               WHERE voucher_type = 'payroll_entry' AND voucher_id = ?
                 AND is_cancelled = 1""",
            (run_id,),
        ).fetchone()["cnt"]
        assert cancelled_gl == original_gl_count

        # Reversal GL entries should exist (not cancelled)
        reversal_gl = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'payroll_entry' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (run_id,),
        ).fetchall()
        assert len(reversal_gl) >= 2

        # Net GL (reversals) should be balanced
        total_dr = sum(Decimal(g["debit"]) for g in reversal_gl)
        total_cr = sum(Decimal(g["credit"]) for g in reversal_gl)
        assert abs(total_dr - total_cr) < Decimal("0.02"), (
            f"Reversal GL not balanced: DR={total_dr}, CR={total_cr}"
        )

        # Overall GL (including cancelled originals + reversals) should net to zero
        all_gl = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'payroll_entry' AND voucher_id = ?""",
            (run_id,),
        ).fetchall()
        net_dr = sum(Decimal(g["debit"]) for g in all_gl)
        net_cr = sum(Decimal(g["credit"]) for g in all_gl)
        # The cancelled originals + their reversals should make the net equal
        # (reversals mirror originals, so total DR == total CR across all entries)
        assert abs(net_dr - net_cr) < Decimal("0.02"), (
            f"Total GL not balanced: DR={net_dr}, CR={net_cr}"
        )
