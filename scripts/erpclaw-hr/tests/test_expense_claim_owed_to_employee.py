"""m805a: an approved expense claim is money owed to the employee.

Once a manager approves an expense claim, the business owes the claimant the
claim total until it is paid. The general ledger already says so (payable
credited, party employee); approval must also write one payment-ledger row for
the claim so get-outstanding shows it, and the party-ledger report must accept
an employee party.

Money discipline: Decimal in Python, TEXT columns, exact two-place string
comparisons. Never float.
"""
import importlib.util
import json
import os

from hr_helpers import build_hr_env, call_action, is_error, is_ok, load_db_query, ns

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPTS_DIR)))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


pay = _load(os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "db_query.py"),
            "db_query_payments_m805a")
rep = _load(os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py"),
            "db_query_reports_m805a")
engine = _load(os.path.join(_ROOT_DIR, "testing", "invariant_engine.py"),
               "invariant_engine_m805a")

CLAIM_DATE = "2026-03-02"


# ── helpers ────────────────────────────────────────────────────────────────

def _ok(result):
    assert is_ok(result), result
    return result


def _employee(conn, env, first, last):
    return _ok(call_action(mod.add_employee, conn, ns(
        first_name=first, last_name=last, date_of_birth=None, gender=None,
        date_of_joining="2025-01-01", employment_type=None,
        company_id=env["company_id"], department_id=None, designation_id=None,
        employee_grade_id=None, branch=None, reporting_to=None, company_email=None,
        personal_email=None, cell_phone=None, emergency_contact=None,
        bank_details=None, ssn=None, federal_filing_status=None, w4_allowances=None,
        holiday_list_id=None, payroll_cost_center_id=None)))["employee_id"]


def _claim(conn, env, employee_id, amount):
    items = [{"expense_type": "travel", "description": "Client visit",
              "amount": amount}]
    return _ok(call_action(mod.add_expense_claim, conn, ns(
        employee_id=employee_id, expense_date=CLAIM_DATE,
        company_id=env["company_id"], items=json.dumps(items))))["expense_claim_id"]


def _submitted(conn, env, employee_id, amount):
    claim_id = _claim(conn, env, employee_id, amount)
    _ok(call_action(mod.submit_expense_claim, conn, ns(expense_claim_id=claim_id)))
    return claim_id


def _approved(conn, env, employee_id, approver_id, amount):
    claim_id = _submitted(conn, env, employee_id, amount)
    result = _ok(call_action(mod.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id, approved_by=approver_id)))
    return claim_id, result


def _ple_rows(conn, claim_id):
    return conn.execute(
        "SELECT * FROM payment_ledger_entry WHERE voucher_id = ?",
        (claim_id,)).fetchall()


def _gl_rows(conn, claim_id):
    return conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ?", (claim_id,)).fetchall()


def _outstanding(conn, employee_id):
    return _ok(call_action(pay.get_outstanding, conn, ns(
        party_type="employee", party_id=employee_id,
        voucher_type=None, voucher_id=None,
        company_id=None, company_name=None)))


def _inv27(conn):
    engine._ensure_decimal_sum(conn)
    return engine._check_inv27_party_level_residual(conn)


def _party_ledger(conn, party_type, party_id):
    return call_action(rep.party_ledger, conn, ns(
        party_type=party_type, party_id=party_id,
        from_date=None, to_date=None, company_id=None, company_name=None))


# ── owed after approve ─────────────────────────────────────────────────────

def test_owed_after_approve(conn, env):
    claimant = _employee(conn, env, "Owed", "Employee")
    approver = _employee(conn, env, "Mara", "Approver")
    claim_id, result = _approved(conn, env, claimant, approver, "150.00")

    status = conn.execute(
        "SELECT status FROM expense_claim WHERE id = ?", (claim_id,)).fetchone()
    assert status["status"] == "approved"
    assert result["total_amount"] == "150.00"
    assert result.get("payment_ledger_entry_id"), result

    ple = _ple_rows(conn, claim_id)
    assert len(ple) == 1
    row = dict(ple[0])
    assert row["amount"] == "150.00"
    assert row["amount_in_account_currency"] == "150.00"
    assert row["account_id"] == env["payable_account"]
    assert (row["party_type"], row["party_id"]) == ("employee", claimant)
    assert (row["voucher_type"], row["against_voucher_type"]) == \
        ("expense_claim", "expense_claim")
    assert (row["voucher_id"], row["against_voucher_id"]) == (claim_id, claim_id)
    assert row["id"] == result["payment_ledger_entry_id"]

    out = _outstanding(conn, claimant)
    assert out["outstanding"] == "150.00"
    assert len(out["vouchers"]) == 1
    voucher = out["vouchers"][0]
    assert voucher["voucher_type"] == "expense_claim"
    assert voucher["voucher_id"] == claim_id
    assert voucher["outstanding_amount"] == "150.00"

    payable_credits = [
        dict(r) for r in _gl_rows(conn, claim_id)
        if r["account_id"] == env["payable_account"]]
    assert len(payable_credits) == 1
    assert payable_credits[0]["credit"] == "150.00"
    assert payable_credits[0]["party_type"] == "employee"
    assert payable_credits[0]["party_id"] == claimant
    assert payable_credits[0]["credit"] == row["amount"]

    assert _inv27(conn) is None


def test_party_ledger_for_employee(conn, env):
    claimant = _employee(conn, env, "Owed", "Employee")
    approver = _employee(conn, env, "Mara", "Approver")
    claim_id, _ = _approved(conn, env, claimant, approver, "150.00")

    result = _ok(_party_ledger(conn, "employee", claimant))
    assert result["party_name"] == "Owed Employee"
    credits = [e for e in result["entries"] if e["voucher_id"] == claim_id]
    assert len(credits) == 1
    assert credits[0]["credit"] == "150.00"
    assert credits[0]["debit"] == "0.00"


# ── not owed before approve ────────────────────────────────────────────────

def test_not_owed_before_approve(conn, env):
    claimant = _employee(conn, env, "Owed", "Employee")
    claim_id = _submitted(conn, env, claimant, "150.00")

    assert _ple_rows(conn, claim_id) == []
    out = _outstanding(conn, claimant)
    assert out["outstanding"] == "0.00"
    assert out["vouchers"] == []


# ── refused approval writes nothing ────────────────────────────────────────

def test_refused_approval_writes_nothing(conn, env):
    claimant = _employee(conn, env, "Owed", "Employee")
    approver = _employee(conn, env, "Mara", "Approver")
    claim_id = _submitted(conn, env, claimant, "150.00")

    conn.execute("DELETE FROM fiscal_year WHERE company_id = ?",
                 (env["company_id"],))
    conn.commit()

    result = call_action(mod.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id, approved_by=approver))
    assert is_error(result)
    assert result["message"] == \
        f"No open fiscal year found for posting date {CLAIM_DATE}"

    assert _ple_rows(conn, claim_id) == []
    assert _gl_rows(conn, claim_id) == []
    status = conn.execute(
        "SELECT status FROM expense_claim WHERE id = ?", (claim_id,)).fetchone()
    assert status["status"] == "submitted"


# ── decoys and second employee ─────────────────────────────────────────────

def test_decoy_claims_do_not_change_outstanding(conn, env):
    claimant = _employee(conn, env, "Owed", "Employee")
    approver = _employee(conn, env, "Mara", "Approver")
    claim_id, _ = _approved(conn, env, claimant, approver, "150.00")

    decoy_submitted = _submitted(conn, env, claimant, "40.00")
    decoy_rejected = _submitted(conn, env, claimant, "25.00")
    _ok(call_action(mod.reject_expense_claim, conn, ns(
        expense_claim_id=decoy_rejected, reason="Duplicate")))
    assert _ple_rows(conn, decoy_submitted) == []
    assert _ple_rows(conn, decoy_rejected) == []

    out = _outstanding(conn, claimant)
    assert out["outstanding"] == "150.00"
    assert [v["voucher_id"] for v in out["vouchers"]] == [claim_id]
    assert _inv27(conn) is None


def test_second_employee_is_isolated(conn, env):
    first = _employee(conn, env, "Owed", "Employee")
    second = _employee(conn, env, "Second", "Employee")
    approver = _employee(conn, env, "Mara", "Approver")
    _, _ = _approved(conn, env, first, approver, "150.00")
    _, _ = _approved(conn, env, second, approver, "80.00")

    assert _outstanding(conn, second)["outstanding"] == "80.00"
    assert _outstanding(conn, first)["outstanding"] == "150.00"
    assert _inv27(conn) is None


# ── INV-27 negative control ────────────────────────────────────────────────

def test_inv27_fires_when_claim_total_is_edited(conn, env):
    claimant = _employee(conn, env, "Owed", "Employee")
    approver = _employee(conn, env, "Mara", "Approver")
    claim_id, _ = _approved(conn, env, claimant, approver, "150.00")
    assert _inv27(conn) is None

    conn.execute("UPDATE expense_claim SET total_amount = '151.00' WHERE id = ?",
                 (claim_id,))
    conn.commit()

    violation = _inv27(conn)
    assert violation is not None
    assert "employee:" in violation
    assert "diff=-1.00" in violation


# ── party ledger refusals ──────────────────────────────────────────────────

def test_party_ledger_refusals(conn, env):
    claimant = _employee(conn, env, "Owed", "Employee")

    unknown = _party_ledger(conn, "employee", "no-such-employee")
    assert is_error(unknown)
    assert unknown["message"] == "Employee no-such-employee not found"

    bogus = _party_ledger(conn, "bogus", claimant)
    assert is_error(bogus)
    assert bogus["message"] == \
        "--party-type must be 'customer', 'supplier' or 'employee'"
