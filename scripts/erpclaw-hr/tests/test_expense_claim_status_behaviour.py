"""Part A: behaviour of reject-expense-claim and update-expense-claim-status,
driven through the claim's real lifecycle and read back from the database.

One claim of 500.00 dated 2026-03-02 for employee Dana Reyes:

  travel  412.35  on "Travel Expense" (named on the item)
  meals    87.65  no account on the item, so approval debits the company's
                  default expense account ("Expense Account")

approve-expense-claim (approver Evan Cole) posts one voucher 'expense_claim':
DR Travel Expense 412.35, DR Expense Account 87.65, CR Accounts Payable 500.00
with the claimant as the party. A rejection posts nothing.

update-expense-claim-status makes only the changes no other action owns and no
ledger posting depends on: a draft or submitted claim (nothing posted yet)
becomes cancelled. An approved claim's payable sits in the payment ledger, so
a bare status change no longer marks it paid — only a submitted payment
allocated to the claim does (m805b). Submission, approval and rejection keep
their own actions, so a claim can neither be marked paid without its approval
posting nor walked back from approved to a status that hides the posting (from
which it could then be rejected with the payable still on the books).
"""
import json
from decimal import Decimal

from hr_helpers import call_action, is_error, is_ok, load_db_query, ns, seed_account

mod = load_db_query()

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


def _setup(conn, env):
    return {
        "dana": _employee(conn, env, "Dana", "Reyes"),
        "evan": _employee(conn, env, "Evan", "Cole"),
        "travel": seed_account(conn, env["company_id"], "Travel Expense",
                               "expense", "expense", "5100"),
    }


def _claim(conn, env, who):
    items = [
        {"expense_type": "travel", "description": "Flight to Denver",
         "amount": "412.35", "account_id": who["travel"]},
        {"expense_type": "meals", "description": "Client dinner", "amount": "87.65"},
    ]
    return _ok(call_action(mod.add_expense_claim, conn, ns(
        employee_id=who["dana"], expense_date=CLAIM_DATE,
        company_id=env["company_id"], items=json.dumps(items))))["expense_claim_id"]


def _submitted(conn, env, who):
    claim_id = _claim(conn, env, who)
    _ok(call_action(mod.submit_expense_claim, conn, ns(expense_claim_id=claim_id)))
    return claim_id


def _approved(conn, env, who):
    claim_id = _submitted(conn, env, who)
    _ok(call_action(mod.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id, approved_by=who["evan"])))
    return claim_id


def _reject(conn, claim_id, reason=None):
    return call_action(mod.reject_expense_claim, conn, ns(
        expense_claim_id=claim_id, reason=reason))


def _set_status(conn, claim_id, status, payment_entry_id=None):
    return call_action(mod.update_expense_claim_status, conn, ns(
        expense_claim_id=claim_id, status=status, payment_entry_id=payment_entry_id))


def _row(conn, claim_id):
    r = conn.execute(
        """SELECT status, total_amount, approved_by, approval_date, payment_entry_id
           FROM expense_claim WHERE id = ?""", (claim_id,)).fetchone()
    return dict(r)


def _items(conn, claim_id):
    rows = conn.execute(
        "SELECT expense_type, amount, account_id FROM expense_claim_item WHERE expense_claim_id = ?",
        (claim_id,)).fetchall()
    return sorted((r["expense_type"], r["amount"], r["account_id"]) for r in rows)


def _gl(conn, claim_id):
    rows = conn.execute(
        """SELECT account_id, debit, credit, voucher_type, is_cancelled, posting_date,
                  party_type, party_id
           FROM gl_entry WHERE voucher_id = ?""", (claim_id,)).fetchall()
    return sorted(tuple(r) for r in rows)


def _audit(conn, claim_id, action):
    rows = conn.execute(
        """SELECT old_values, new_values FROM audit_log
           WHERE entity_type = 'expense_claim' AND entity_id = ? AND action = ?""",
        (claim_id, action)).fetchall()
    return [(json.loads(r["old_values"]), json.loads(r["new_values"])) for r in rows]


def _counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM expense_claim").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM expense_claim_item").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
    )


def _posting(env, who):
    return sorted([
        (who["travel"], "412.35", "0.00", "expense_claim", 0, CLAIM_DATE, None, None),
        (env["expense_account"], "87.65", "0.00", "expense_claim", 0, CLAIM_DATE, None, None),
        (env["payable_account"], "0.00", "500.00", "expense_claim", 0, CLAIM_DATE,
         "employee", who["dana"]),
    ])


# ── reject-expense-claim ───────────────────────────────────────────────────

def test_reject_submitted_claim_stores_rejected_and_posts_nothing(conn, env):
    who = _setup(conn, env)
    claim_id = _submitted(conn, env, who)
    assert _row(conn, claim_id) == {"status": "submitted", "total_amount": "500.00",
                                    "approved_by": None, "approval_date": None,
                                    "payment_entry_id": None}
    assert _items(conn, claim_id) == sorted([("travel", "412.35", who["travel"]),
                                             ("meals", "87.65", None)])

    r = _reject(conn, claim_id, "Receipts missing")
    assert is_ok(r)
    assert (r["expense_claim_id"], r["employee_id"], r["rejection_reason"]) == \
        (claim_id, who["dana"], "Receipts missing")

    assert _row(conn, claim_id) == {"status": "rejected", "total_amount": "500.00",
                                    "approved_by": None, "approval_date": None,
                                    "payment_entry_id": None}
    assert _gl(conn, claim_id) == []
    assert _audit(conn, claim_id, "reject-expense-claim") == [
        ({"status": "submitted"},
         {"status": "rejected", "rejection_reason": "Receipts missing"})]

    other = _submitted(conn, env, who)
    r = _reject(conn, other)
    assert r["rejection_reason"] == "No reason provided"
    assert _row(conn, other)["status"] == "rejected"
    assert _gl(conn, other) == []


def test_reject_refusals_write_nothing(conn, env):
    who = _setup(conn, env)
    draft = _claim(conn, env, who)
    approved = _approved(conn, env, who)
    paid = _approved(conn, env, who)
    # m805b: the bare approved -> paid mark is refused for a claim the ledger
    # owes to the employee, so no claim reaches 'paid' through this action.
    # Rejection still refuses every non-submitted claim and writes nothing.
    r = _set_status(conn, paid, "paid", "PAY-0001")
    assert is_error(r)
    assert r["message"] == (
        f"Expense claim {paid} is owed to the employee in the payment "
        "ledger; it is marked paid when a submitted payment is allocated to it")
    rejected = _submitted(conn, env, who)
    _ok(_reject(conn, rejected, "Duplicate"))

    assert _gl(conn, approved) == _posting(env, who)
    counts = _counts(conn)

    for claim_id, status in ((draft, "draft"), (approved, "approved"),
                             (paid, "approved"), (rejected, "rejected")):
        r = _reject(conn, claim_id, "Too late")
        assert is_error(r)
        assert r["message"] == (f"Expense claim {claim_id} cannot be rejected. "
                                f"Current status: {status} (must be 'submitted')")
        assert _row(conn, claim_id)["status"] == status

    r = _reject(conn, None)
    assert is_error(r) and r["message"] == "--expense-claim-id is required"
    r = _reject(conn, "no-such-claim")
    assert is_error(r) and r["message"] == "Expense claim no-such-claim not found"

    assert _counts(conn) == counts
    assert _gl(conn, approved) == _posting(env, who)
    assert _gl(conn, paid) == _posting(env, who)
    assert _row(conn, paid)["payment_entry_id"] is None


# ── update-expense-claim-status ────────────────────────────────────────────

def test_approved_claim_is_marked_paid_over_its_single_posting(conn, env):
    who = _setup(conn, env)
    claim_id = _approved(conn, env, who)

    gl = _gl(conn, claim_id)
    assert gl == _posting(env, who)
    assert sum(Decimal(g[1]) for g in gl) == sum(Decimal(g[2]) for g in gl) == Decimal("500.00")
    row = _row(conn, claim_id)
    assert (row["status"], row["approved_by"], row["payment_entry_id"]) == \
        ("approved", who["evan"], None)

    # m805b: the approval posted the payable to the payment ledger, so a bare
    # status change no longer marks the claim paid — only a submitted payment
    # allocated to the claim does. The refusal changes nothing: the claim, its
    # single posting and the audit trail stand exactly as approved.
    counts = _counts(conn)
    r = _set_status(conn, claim_id, "paid", "PAY-0042")
    assert is_error(r)
    assert r["message"] == (
        f"Expense claim {claim_id} is owed to the employee in the payment "
        "ledger; it is marked paid when a submitted payment is allocated to it")

    row = _row(conn, claim_id)
    assert (row["status"], row["total_amount"], row["approved_by"], row["payment_entry_id"]) == \
        ("approved", "500.00", who["evan"], None)
    assert _gl(conn, claim_id) == _posting(env, who)
    assert _audit(conn, claim_id, "update-expense-claim-status") == []
    assert _counts(conn) == counts


def test_unposted_claims_can_be_cancelled(conn, env):
    who = _setup(conn, env)
    draft = _claim(conn, env, who)
    submitted = _submitted(conn, env, who)

    for claim_id, old in ((draft, "draft"), (submitted, "submitted")):
        r = _set_status(conn, claim_id, "cancelled")
        assert is_ok(r)
        assert (r["old_status"], r["new_status"]) == (old, "cancelled")
        assert _row(conn, claim_id) == {"status": "cancelled", "total_amount": "500.00",
                                        "approved_by": None, "approval_date": None,
                                        "payment_entry_id": None}
        assert _gl(conn, claim_id) == []


def test_status_update_cannot_skip_or_undo_the_ledger_path(conn, env):
    who = _setup(conn, env)
    claims = {
        "draft": _claim(conn, env, who),
        "submitted": _submitted(conn, env, who),
        "approved": _approved(conn, env, who),
        "rejected": _submitted(conn, env, who),
        "cancelled": _claim(conn, env, who),
    }
    _ok(_reject(conn, claims["rejected"], "Duplicate"))
    _ok(_set_status(conn, claims["cancelled"], "cancelled"))
    # m805b: no claim reaches 'paid' through this action any more — the bare
    # approved -> paid mark is refused for a claim the ledger owes to the
    # employee (pinned in test_approved_claim_is_marked_paid_over_its_single_posting),
    # so the 'paid' transition-out entries live in the payments suite now.
    counts = _counts(conn)

    refused = [
        ("draft", "paid"), ("draft", "submitted"), ("draft", "approved"),
        ("submitted", "paid"), ("submitted", "approved"), ("submitted", "rejected"),
        ("approved", "draft"), ("approved", "submitted"), ("approved", "cancelled"),
        ("approved", "approved"),
        ("rejected", "paid"), ("rejected", "draft"), ("rejected", "cancelled"),
        ("cancelled", "draft"), ("cancelled", "paid"),
    ]
    allowed = {"draft": "cancelled", "submitted": "cancelled", "approved": "paid"}
    for old, new in refused:
        claim_id = claims[old]
        r = _set_status(conn, claim_id, new, "PAY-9999")
        assert is_error(r), (old, new, r)
        assert r["message"] == (
            f"Expense claim {claim_id} cannot change from '{old}' to '{new}'. "
            f"Allowed from '{old}': {allowed.get(old, 'none')}"), (old, new)
        assert _row(conn, claim_id)["status"] == old

    assert _counts(conn) == counts
    for key in ("draft", "submitted", "rejected", "cancelled"):
        assert _gl(conn, claims[key]) == []
    assert _gl(conn, claims["approved"]) == _posting(env, who)


def test_approved_claim_cannot_be_walked_back_under_its_posting(conn, env):
    who = _setup(conn, env)
    claim_id = _approved(conn, env, who)

    walked_back = _set_status(conn, claim_id, "submitted")
    rejected = _reject(conn, claim_id, "Changed plans")

    # The approval posting stands, so the claim must still read approved.
    assert _gl(conn, claim_id) == _posting(env, who)
    assert _row(conn, claim_id)["status"] == "approved"
    assert is_error(walked_back)
    assert rejected["message"] == (f"Expense claim {claim_id} cannot be rejected. "
                                   f"Current status: approved (must be 'submitted')")
    approved_again = call_action(mod.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id, approved_by=who["evan"]))
    assert approved_again["message"] == (f"Expense claim {claim_id} cannot be approved. "
                                         f"Current status: approved (must be 'submitted')")
    assert _gl(conn, claim_id) == _posting(env, who)


def test_status_update_refusals_write_nothing(conn, env):
    who = _setup(conn, env)
    claim_id = _approved(conn, env, who)
    counts = _counts(conn)

    r = _set_status(conn, None, "paid")
    assert is_error(r) and r["message"] == "--expense-claim-id is required"
    r = _set_status(conn, claim_id, None)
    assert is_error(r) and r["message"] == "--status is required"
    r = _set_status(conn, claim_id, "settled")
    assert is_error(r)
    assert r["message"] == ("Invalid expense claim status 'settled'. Valid: "
                            "('draft', 'submitted', 'approved', 'rejected', 'paid', 'cancelled')")
    r = _set_status(conn, "no-such-claim", "paid")
    assert is_error(r) and r["message"] == "Expense claim no-such-claim not found"

    assert _counts(conn) == counts
    assert _row(conn, claim_id)["status"] == "approved"
