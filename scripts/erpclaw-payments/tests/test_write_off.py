"""Part A — Wave G F17a: ``write-off-invoice``, the standalone clearing primitive.

Ruling N6a puts the general AR write-off inside the correctness floor, un-gated,
single invoice, explicit amount, **no batch entry point**. The mechanism is its
own primitive rather than a fabricated zero-cash payment, because the shipped
payment path structurally refuses that shape (Wave-G SIM finding 4 / correction
C4): ``--paid-amount must be > 0``, the non-negative residual gate, and
``payment_deduction.payment_entry_id NOT NULL``.

Every pin here drives the REAL action against a fresh core DB and asserts exact
Decimals. The invoice's own primary GL is posted through the shared
``insert_gl_entries`` so INV-01 / INV-02 / INV-17 are measuring a real book and
so the cancel pin can prove the "zero new logic" claim against the REAL
``cancel-sales-invoice``, not a stub.

Pin map (plan §4 F17 "Part A pins"):
  1. 340.00 written off a 1,000.00 invoice ⇒ outstanding 660.00, exact GL pair,
     PLE net 660.00, INV-01/02/17/25/27 green.
  2. full residual written off ⇒ 'paid' via the EXISTING status sync, no orphan
     ledger row, INV-22 green.
  3. cancel a written-off invoice ⇒ GL fully reversed INCLUDING the write-off
     entry_set, every PLE row for the voucher delinked, INV-05 green.
  5. F17c's core half (the legalclaw delegation) is pinned in
     source/legalclaw/scripts/tests/test_timebilling.py.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

from payments_helpers import (build_ap_env, build_ar_env, call_action, is_error,
                              is_ok, load_db_query, ns, seed_account,
                              seed_purchase_invoice, seed_sales_invoice)

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))  # scripts/

D = Decimal


def _find_repo_root(start):
    cur = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(cur, "CLAUDE.md")) or \
                os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise RuntimeError(f"repo root not found from {start}")
        cur = parent


try:
    _REPO_ROOT = _find_repo_root(_TESTS_DIR)
except RuntimeError:
    _REPO_ROOT = ""
_INV_PATH = os.path.join(_REPO_ROOT, "testing", "invariant_engine.py") \
    if _REPO_ROOT else ""
if _INV_PATH and os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_f17", _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None


def _load_sibling(domain):
    """Load a sibling foundation domain script by path.

    Pin 3's whole claim is that cancel-invoice needs ZERO new logic, so it has to
    run the REAL erpclaw-selling cancel path. Loading it by explicit path is the
    same idiom load_db_query() uses and avoids a sys.path collision between two
    modules that both name their entry point db_query.py.
    """
    path = os.path.join(_SCRIPTS_DIR, domain, "db_query.py")
    spec = importlib.util.spec_from_file_location(f"db_query_{domain}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inv(conn, name):
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    inv_engine._ensure_decimal_sum(conn)
    return getattr(inv_engine, name)(conn)


def _all_green(conn, *names):
    """Assert a set of invariants, reporting the first real failure detail."""
    for name in names:
        detail = _inv(conn, name)
        assert detail is None, f"{name}: {detail}"


_CORE = ("_check_inv01_global_double_entry",
         "_check_inv02_per_voucher_balance",
         "_check_inv17_trial_balance_zero",
         "_check_inv25_ar_summary_detail",
         "_check_inv27_party_level_residual")


# ── readers ──────────────────────────────────────────────────────────────────

def _outstanding(conn, table, doc_id):
    return D(conn.execute(
        f"SELECT outstanding_amount FROM {table} WHERE id = ?",
        (doc_id,)).fetchone()[0])


def _doc_status(conn, table, doc_id):
    return conn.execute(f"SELECT status FROM {table} WHERE id = ?",
                        (doc_id,)).fetchone()[0]


def _voucher_ple_net(conn, voucher_type, voucher_id):
    """The invoice's own INV-25 reading: live document rows + all payment rows."""
    net = D("0")
    for amount in conn.execute(
            "SELECT amount FROM payment_ledger_entry "
            " WHERE (voucher_type = ? AND voucher_id = ? AND delinked = 0) "
            "    OR (voucher_type = 'payment_entry' "
            "        AND against_voucher_type = ? AND against_voucher_id = ?)",
            (voucher_type, voucher_id, voucher_type, voucher_id)):
        net += D(amount[0])
    return net


def _gl_legs(conn, voucher_type, voucher_id, entry_set):
    return conn.execute(
        "SELECT account_id, debit, credit, party_type, party_id, cost_center_id,"
        "       is_cancelled, entry_set "
        "  FROM gl_entry WHERE voucher_type = ? AND voucher_id = ? "
        "   AND entry_set = ? ORDER BY CAST(debit AS NUMERIC) DESC, id",
        (voucher_type, voucher_id, entry_set)).fetchall()


def _write_off_ple(conn, voucher_type, voucher_id):
    return conn.execute(
        "SELECT id, amount, account_id, party_type, party_id, delinked, "
        "       against_voucher_type, against_voucher_id "
        "  FROM payment_ledger_entry "
        " WHERE voucher_type = ? AND voucher_id = ? AND remarks LIKE 'Write-off:%'",
        (voucher_type, voucher_id)).fetchall()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ── fixtures driven through the real writers ─────────────────────────────────

@pytest.fixture
def env(conn):
    """AR env + an income account and a bad-debt expense account."""
    e = build_ar_env(conn)
    e["income"] = seed_account(conn, e["company_id"], "Sales", "income")
    e["bad_debt"] = seed_account(conn, e["company_id"], "Bad Debt Expense",
                                 "expense")
    return e


@pytest.fixture
def apenv(conn):
    e = build_ap_env(conn)
    e["expense"] = seed_account(conn, e["company_id"], "Cost of Sales", "expense")
    e["forgiven"] = seed_account(conn, e["company_id"],
                                 "Gain on Debt Forgiveness", "income")
    return e


def _post_invoice_gl(conn, env, voucher_type, voucher_id, amount, *,
                     control, other, party_type, party_id, control_debit):
    """Post the invoice's OWN primary GL set through the shared lib.

    seed_sales_invoice / seed_purchase_invoice write the document row and its
    ledger row but no GL, which is right for the F2 suite and wrong here: F17a's
    claims are GL claims, and INV-01/02/17 measure a fixture rather than a book
    unless the invoice's own pair exists.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(_SCRIPTS_DIR), "scripts",
                                    "erpclaw-setup", "lib"))
    from erpclaw_lib.gl_posting import insert_gl_entries
    control_leg = {"account_id": control,
                   "debit": amount if control_debit else "0",
                   "credit": "0" if control_debit else amount,
                   "party_type": party_type, "party_id": party_id,
                   "fiscal_year": "FY"}
    other_leg = {"account_id": other,
                 "debit": "0" if control_debit else amount,
                 "credit": amount if control_debit else "0",
                 "cost_center_id": env["cc"], "fiscal_year": "FY"}
    insert_gl_entries(conn, [control_leg, other_leg],
                      voucher_type=voucher_type, voucher_id=voucher_id,
                      posting_date="2026-06-01", company_id=env["company_id"],
                      remarks=f"{voucher_type} {voucher_id}")
    conn.commit()


def _ar_invoice(conn, env, grand_total):
    si = seed_sales_invoice(conn, env, grand_total)
    _post_invoice_gl(conn, env, "sales_invoice", si, grand_total,
                     control=env["ar"], other=env["income"],
                     party_type="customer", party_id=env["customer"],
                     control_debit=True)
    return si


def _ap_invoice(conn, apenv, grand_total):
    pi = seed_purchase_invoice(conn, apenv, grand_total)
    _post_invoice_gl(conn, apenv, "purchase_invoice", pi, grand_total,
                     control=apenv["ap"], other=apenv["expense"],
                     party_type="supplier", party_id=apenv["supplier"],
                     control_debit=False)
    return pi


def _write_off(conn, *, voucher_type, voucher_id, amount, account,
               reason="Customer insolvent — 2026 bad debt review",
               posting_date=None, cost_center_id=None):
    return call_action(mod.write_off_invoice, conn, ns(
        voucher_type=voucher_type, voucher_id=voucher_id,
        write_off_amount=amount, write_off_account_id=account,
        reason=reason, posting_date=posting_date,
        cost_center_id=cost_center_id))


def _add_receive(conn, env, amount, allocations=None, deductions=None):
    """Draft a receive payment. Returns the raw add-payment result (pin 4 asserts
    on the refusal case too, so this must not assert success itself)."""
    return call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=json.dumps(deductions) if deductions else None))


def _receive(conn, env, amount, allocations=None, deductions=None):
    created = _add_receive(conn, env, amount, allocations, deductions)
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


# ── pin 1 — 340.00 of 1,000.00, every figure exact ───────────────────────────

def test_pin1_partial_write_off_exact_decimals(conn, env, capsys):
    """1,000.00 open invoice, 340.00 written off.

    outstanding 660.00, one balanced GL pair (DR bad debt 340.00 / CR the
    invoice's own receivable 340.00, party-stamped), one ledger row of −340.00
    under the invoice's own voucher with NO against-voucher, and the five
    invariants the plan names, all green.
    """
    si = _ar_invoice(conn, env, "1000.00")
    assert _outstanding(conn, "sales_invoice", si) == D("1000.00")

    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="340.00", account=env["bad_debt"])
    assert is_ok(res), res
    print(f"\npin 1 — {json.dumps(res, indent=2)}")

    assert res["write_off_amount"] == "340.00"
    assert res["outstanding_amount"] == "660.00"
    assert res["invoice_status"] == "partially_paid"
    assert res["gl_entries_created"] == 2

    assert _outstanding(conn, "sales_invoice", si) == D("660.00")
    assert _doc_status(conn, "sales_invoice", si) == "partially_paid"

    legs = _gl_legs(conn, "sales_invoice", si, "write_off")
    assert len(legs) == 2
    dr, cr = legs[0], legs[1]
    assert dr["account_id"] == env["bad_debt"]
    assert D(dr["debit"]) == D("340.00") and D(dr["credit"]) == D("0")
    assert dr["party_type"] is None and dr["party_id"] is None
    # A P&L leg without a cost center fails 12-step step 6; it is stamped here.
    assert dr["cost_center_id"] == env["cc"]
    assert cr["account_id"] == env["ar"]
    assert D(cr["credit"]) == D("340.00") and D(cr["debit"]) == D("0")
    assert cr["party_type"] == "customer" and cr["party_id"] == env["customer"]
    assert D(dr["debit"]) - D(cr["credit"]) == D("0"), "the pair must balance"

    rows = _write_off_ple(conn, "sales_invoice", si)
    assert len(rows) == 1, "exactly ONE ledger row per write-off"
    row = rows[0]
    assert D(row["amount"]) == D("-340.00")
    assert row["account_id"] == env["ar"], "same control account as the GL leg"
    assert row["party_type"] == "customer" and row["party_id"] == env["customer"]
    assert row["delinked"] == 0
    assert row["against_voucher_type"] is None
    assert row["against_voucher_id"] is None

    assert _voucher_ple_net(conn, "sales_invoice", si) == D("660.00")
    _all_green(conn, *_CORE)


def test_pin1_writes_no_payment_and_no_deduction_row(conn, env):
    """Correction C4's whole point: this primitive fabricates no cash document."""
    si = _ar_invoice(conn, env, "1000.00")
    before = (_count(conn, "payment_entry"), _count(conn, "payment_deduction"),
              _count(conn, "payment_allocation"))
    assert is_ok(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                            amount="340.00", account=env["bad_debt"]))
    assert (_count(conn, "payment_entry"), _count(conn, "payment_deduction"),
            _count(conn, "payment_allocation")) == before == (0, 0, 0)


def test_pin1_ap_side_mirrors(conn, apenv):
    """A supplier balance written off: DR the payable, CR the gain account."""
    pi = _ap_invoice(conn, apenv, "1000.00")
    res = _write_off(conn, voucher_type="purchase_invoice", voucher_id=pi,
                     amount="340.00", account=apenv["forgiven"])
    assert is_ok(res), res
    assert res["outstanding_amount"] == "660.00"

    legs = _gl_legs(conn, "purchase_invoice", pi, "write_off")
    assert len(legs) == 2
    dr = [r for r in legs if D(r["debit"]) > 0][0]
    cr = [r for r in legs if D(r["credit"]) > 0][0]
    assert dr["account_id"] == apenv["ap"]
    assert D(dr["debit"]) == D("340.00")
    assert dr["party_type"] == "supplier" and dr["party_id"] == apenv["supplier"]
    assert cr["account_id"] == apenv["forgiven"]
    assert D(cr["credit"]) == D("340.00")
    assert cr["cost_center_id"] == apenv["cc"]

    assert _voucher_ple_net(conn, "purchase_invoice", pi) == D("660.00")
    _all_green(conn, *_CORE)


# ── pin 2 — the full residual reaches 'paid' through the EXISTING sync ───────

def test_pin2_full_residual_write_off_marks_paid(conn, env):
    """1,000.00 invoice, 300.00 paid, the 700.00 residual written off.

    No bespoke status branch: apply_payment_to_document's own rule takes the
    invoice to 'paid' with outstanding '0'. INV-22 (paid ⇒ ledger nets zero) is
    asserted because that is the invariant a wrong status would break.
    """
    si = _ar_invoice(conn, env, "1000.00")
    _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    assert _outstanding(conn, "sales_invoice", si) == D("700.00")

    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="700.00", account=env["bad_debt"])
    assert is_ok(res), res
    assert res["outstanding_amount"] == "0"
    assert res["invoice_status"] == "paid"
    assert _doc_status(conn, "sales_invoice", si) == "paid"
    assert _voucher_ple_net(conn, "sales_invoice", si) == D("0")
    assert len(_write_off_ple(conn, "sales_invoice", si)) == 1, "no orphan row"

    _all_green(conn, *_CORE, "_check_inv22_payment_invoice_reconciliation")


def test_pin2_whole_invoice_written_off_from_open(conn, env):
    """The simplest full write-off: nothing paid, the entire 1,000.00 forgiven."""
    si = _ar_invoice(conn, env, "1000.00")
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="1000.00", account=env["bad_debt"])
    assert is_ok(res), res
    assert res["outstanding_amount"] == "0"
    assert _doc_status(conn, "sales_invoice", si) == "paid"
    assert _voucher_ple_net(conn, "sales_invoice", si) == D("0")
    _all_green(conn, *_CORE, "_check_inv22_payment_invoice_reconciliation")


# ── pin 3 — cancel reverses it with ZERO new logic ──────────────────────────

def test_pin3_cancel_invoice_reverses_the_write_off(conn, env, capsys):
    """The REAL cancel-sales-invoice, unmodified, reverses both entry sets.

    reverse_gl_entries() takes every active row for (voucher_type, voucher_id)
    regardless of entry_set, and the cancel path delinks every ledger row for
    that voucher — which is exactly why F17a posts under the invoice's own
    voucher instead of inventing one. Nothing in erpclaw-selling knows this
    action exists.
    """
    selling = _load_sibling("erpclaw-selling")
    si = _ar_invoice(conn, env, "1000.00")
    assert is_ok(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                            amount="340.00", account=env["bad_debt"]))

    active_before = conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_type='sales_invoice' "
        "AND voucher_id=? AND is_cancelled=0", (si,)).fetchone()[0]
    assert active_before == 4, "invoice pair + write-off pair"

    cancelled = call_action(selling.cancel_sales_invoice, conn,
                            ns(sales_invoice_id=si))
    assert is_ok(cancelled), cancelled
    print(f"\npin 3 — {json.dumps(cancelled, indent=2)}")
    assert cancelled["gl_reversals"] == 4

    still_active = conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_type='sales_invoice' "
        "AND voucher_id=? AND is_cancelled=0 AND id NOT IN "
        "(SELECT id FROM gl_entry WHERE remarks LIKE 'Reversal of %')",
        (si,)).fetchone()[0]
    assert still_active == 0, "every original entry is cancelled"

    # The write-off pair's reversal preserved its entry_set (reverse_gl_entries
    # copies it), so the set nets to zero rather than vanishing.
    wo_net = D("0")
    for debit, credit in conn.execute(
            "SELECT debit, credit FROM gl_entry WHERE voucher_type='sales_invoice'"
            " AND voucher_id=? AND entry_set='write_off'", (si,)):
        wo_net += D(debit) - D(credit)
    assert wo_net == D("0")

    live = conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry WHERE voucher_type="
        "'sales_invoice' AND voucher_id=? AND delinked=0", (si,)).fetchone()[0]
    assert live == 0, "every ledger row for the voucher is delinked, write-off included"

    _all_green(conn, "_check_inv05_cancellation_symmetry",
               "_check_inv01_global_double_entry",
               "_check_inv02_per_voucher_balance",
               "_check_inv17_trial_balance_zero",
               "_check_inv25_ar_summary_detail",
               "_check_inv27_party_level_residual")


# ── posting date — the write-off is dated by the DECISION ───────────────────
#
# QA condition 3 / pm ruling 2026-08-08. The default used to be the INVOICE's
# posting date, which backdated bad-debt expense into the period the sale was
# made — a 2026 collections review restating 2025 — and became an outright
# blocker once that year closed (12-step validation step 9 refuses a closed
# fiscal year, so the write-off was impossible through the documented flag set).
# Default is now today; `--posting-date` stays as the explicit backdate override.


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_open_fy_for_today(conn, env):
    """Guarantee an OPEN fiscal year covering today, whenever 'today' is.

    The AR fixture's fiscal year is a fixed 2026 span; deriving this from the
    same clock the action reads keeps the pins from turning into a time bomb.
    """
    today = _today()
    row = conn.execute(
        "SELECT id FROM fiscal_year WHERE start_date <= ? AND end_date >= ? "
        "  AND is_closed = 0 AND company_id = ?",
        (today, today, env["company_id"])).fetchone()
    if row:
        return
    year = today[:4]
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, "
        " company_id) VALUES (?, ?, ?, ?, 0, ?)",
        (str(uuid.uuid4()), f"FY-{year}", f"{year}-01-01", f"{year}-12-31",
         env["company_id"]))
    conn.commit()


def _gl_posting_dates(conn, voucher_type, voucher_id, entry_set):
    return {r["posting_date"] for r in conn.execute(
        "SELECT posting_date FROM gl_entry WHERE voucher_type = ? "
        " AND voucher_id = ? AND entry_set = ?",
        (voucher_type, voucher_id, entry_set))}


def test_write_off_defaults_to_today_not_the_invoice_date(conn, env):
    """The decision date, not the sale's date."""
    _ensure_open_fy_for_today(conn, env)
    si = _ar_invoice(conn, env, "1000.00")
    invoice_date = conn.execute(
        "SELECT posting_date FROM sales_invoice WHERE id = ?", (si,)).fetchone()[0]
    assert invoice_date == "2026-06-01", "fixture precondition"

    assert is_ok(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                            amount="340.00", account=env["bad_debt"]))

    today = _today()
    assert _gl_posting_dates(conn, "sales_invoice", si, "write_off") == {today}
    assert conn.execute(
        "SELECT posting_date FROM payment_ledger_entry WHERE voucher_type="
        "'sales_invoice' AND voucher_id = ? AND remarks LIKE 'Write-off:%'",
        (si,)).fetchone()[0] == today
    _all_green(conn, *_CORE)


def test_explicit_posting_date_overrides_the_default(conn, env):
    """A deliberate backdate is still available — it just is not the default."""
    _ensure_open_fy_for_today(conn, env)
    si = _ar_invoice(conn, env, "1000.00")
    assert is_ok(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                            amount="340.00", account=env["bad_debt"],
                            posting_date="2026-06-15"))
    assert _gl_posting_dates(conn, "sales_invoice", si, "write_off") == \
        {"2026-06-15"}


def test_an_invoice_in_a_closed_year_can_still_be_written_off(conn, env):
    """The behaviour the old default made impossible.

    Invoice sits in a CLOSED fiscal year; 12-step step 9 refuses any posting
    there. Under the old invoice-date default the write-off was unreachable
    through the documented flag set. Dating it by the decision fixes that
    without weakening step 9, which still guards the closed year itself.
    """
    _ensure_open_fy_for_today(conn, env)
    si = _ar_invoice(conn, env, "1000.00")
    conn.execute("UPDATE sales_invoice SET posting_date = '2025-06-01' WHERE id = ?",
                 (si,))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, "
        " company_id) VALUES (?, 'FY-2025', '2025-01-01', '2025-12-31', 1, ?)",
        (str(uuid.uuid4()), env["company_id"]))
    conn.commit()

    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="340.00", account=env["bad_debt"])
    assert is_ok(res), res
    assert _outstanding(conn, "sales_invoice", si) == D("660.00")
    assert _gl_posting_dates(conn, "sales_invoice", si, "write_off") == {_today()}

    # Step 9 is not weakened: an explicit backdate into the closed year is
    # still refused, and refused without writing.
    si2 = _ar_invoice(conn, env, "500.00")
    conn.execute("UPDATE sales_invoice SET posting_date = '2025-06-01' WHERE id = ?",
                 (si2,))
    conn.commit()
    refused = _write_off(conn, voucher_type="sales_invoice", voucher_id=si2,
                         amount="100.00", account=env["bad_debt"],
                         posting_date="2025-06-01")
    assert is_error(refused) and "closed" in refused["message"]
    assert _gl_legs(conn, "sales_invoice", si2, "write_off") == []


# ── pin 4 — F17b: `write_off` as a deduction taken AT PAYMENT TIME ───────────
#
# The OTHER case the roadmap row named, and a different mechanism entirely: real
# cash arrives, the residual is forgiven on the spot, and the shipped pro-rata
# deduction machinery clears the invoice because the allocation is present. The
# migration half (CHECK rejects pre-034, admits post-034) is pinned in
# erpclaw-setup/tests/test_migration_034_write_off_deduction.py; what is pinned
# HERE is the half the CHANGELOG actually promises a user — that the value is
# reachable through the real entry point and that the books come out right.

def test_pin4_write_off_deduction_at_payment_time_clears_the_invoice(conn, env):
    """950.00 wired against a 1,000.00 invoice, 50.00 written off at payment time.

    Every figure exact: the invoice closes at 'paid' with outstanding '0', the
    deduction posts its own expense leg beside a shrunken cash leg, and the AR
    credit still carries the FULL 1,000.00 so debits equal credits.
    """
    si = _ar_invoice(conn, env, "1000.00")

    created = _add_receive(conn, env, "1000.00",
                           allocations=[{"voucher_type": "sales_invoice",
                                         "voucher_id": si,
                                         "allocated_amount": "950.00"}],
                           deductions=[{"account_id": env["bad_debt"],
                                        "amount": "50.00",
                                        "type": "write_off",
                                        "description": "uncollectable residual"}])
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    # paid (1,000.00) = allocations (950.00) + deductions (50.00) + unallocated (0)
    assert D(conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe_id,)).fetchone()[0]) == D("0")

    submitted = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(submitted), submitted
    assert submitted["deductions"] == {"total": "50.00", "count": 1}

    # The 50.00 rode the clearing pro-rata: the invoice is fully closed.
    assert _outstanding(conn, "sales_invoice", si) == D("0")
    assert _doc_status(conn, "sales_invoice", si) == "paid"
    assert _voucher_ple_net(conn, "sales_invoice", si) == D("0")

    legs = {r["account_id"]: r for r in conn.execute(
        "SELECT account_id, debit, credit, cost_center_id FROM gl_entry "
        " WHERE voucher_type = 'payment_entry' AND voucher_id = ? "
        "   AND is_cancelled = 0", (pe_id,))}
    assert len(legs) == 3
    assert D(legs[env["bank"]]["debit"]) == D("950.00")
    assert D(legs[env["bad_debt"]]["debit"]) == D("50.00")
    assert legs[env["bad_debt"]]["cost_center_id"] == env["cc"], \
        "the write-off expense leg is P&L; 12-step step 6 wants its cost center"
    assert D(legs[env["ar"]]["credit"]) == D("1000.00")
    assert sum(D(r["debit"]) for r in legs.values()) == \
        sum(D(r["credit"]) for r in legs.values()) == D("1000.00")

    # It really is stored and surfaced as a write-off, not silently coerced.
    fetched = call_action(mod.get_payment, conn, ns(payment_entry_id=pe_id))
    assert [(d["type"], d["amount"]) for d in fetched["deductions"]] == \
        [("write_off", "50.00")]

    _all_green(conn, *_CORE, "_check_inv22_payment_invoice_reconciliation")


def test_pin4_the_deduction_write_off_creates_no_write_off_gl_entry_set(conn, env):
    """F17a and F17b are different mechanisms and must stay distinguishable.

    The payment-time write-off is a deduction leg on the PAYMENT's voucher; it
    must NOT masquerade as F17a's entry_set on the invoice, or a later reader
    cannot tell a forgiven residual from a bad-debt provision.
    """
    si = _ar_invoice(conn, env, "1000.00")
    _receive(conn, env, "1000.00",
             allocations=[{"voucher_type": "sales_invoice", "voucher_id": si,
                           "allocated_amount": "950.00"}],
             deductions=[{"account_id": env["bad_debt"], "amount": "50.00",
                          "type": "write_off"}])
    assert _gl_legs(conn, "sales_invoice", si, "write_off") == []
    assert _write_off_ple(conn, "sales_invoice", si) == []


def test_pin4_cancelling_the_payment_reverses_the_write_off_deduction(conn, env):
    """Immutable GL: the deduction leg is reversed, never edited, and the
    invoice's outstanding comes back to the full 1,000.00."""
    si = _ar_invoice(conn, env, "1000.00")
    pe_id = _receive(conn, env, "1000.00",
                     allocations=[{"voucher_type": "sales_invoice",
                                   "voucher_id": si,
                                   "allocated_amount": "950.00"}],
                     deductions=[{"account_id": env["bad_debt"],
                                  "amount": "50.00", "type": "write_off"}])

    cancelled = call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(cancelled), cancelled
    assert _outstanding(conn, "sales_invoice", si) == D("1000.00")
    assert conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_type='payment_entry' "
        "AND voucher_id=? AND account_id=? AND is_cancelled=0 "
        "AND remarks NOT LIKE 'Reversal of %'",
        (pe_id, env["bad_debt"])).fetchone()[0] == 0
    _all_green(conn, "_check_inv05_cancellation_symmetry", *_CORE)


def test_pin4_the_widen_admits_write_off_and_nothing_else(conn, env):
    """Migration 034 widened the vocabulary by exactly one value.

    A gate that started accepting anything would pass the pin above and still be
    wrong, so the negative control runs on the same real entry point.
    """
    assert "write_off" in mod.VALID_DEDUCTION_TYPES
    si = _ar_invoice(conn, env, "1000.00")
    rejected = _add_receive(conn, env, "1000.00",
                            allocations=[{"voucher_type": "sales_invoice",
                                          "voucher_id": si,
                                          "allocated_amount": "950.00"}],
                            deductions=[{"account_id": env["bad_debt"],
                                         "amount": "50.00",
                                         "type": "writeoff"}])
    assert is_error(rejected) and "Invalid deduction type" in rejected["message"]
    assert _count(conn, "payment_deduction") == 0


# ── refusals: every one is loud, none writes anything ────────────────────────

def test_refuses_over_application(conn, env):
    si = _ar_invoice(conn, env, "1000.00")
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="1000.01", account=env["bad_debt"])
    assert is_error(res) and "exceeds outstanding" in res["message"]
    assert _outstanding(conn, "sales_invoice", si) == D("1000.00")
    assert _gl_legs(conn, "sales_invoice", si, "write_off") == []


def test_refuses_a_second_write_off_with_the_amount_already_taken(conn, env):
    """insert_gl_entries is idempotent per entry_set; the operator is told why."""
    si = _ar_invoice(conn, env, "1000.00")
    assert is_ok(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                            amount="340.00", account=env["bad_debt"]))
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="100.00", account=env["bad_debt"])
    assert is_error(res)
    assert "already carries a write-off of 340.00" in res["message"]
    assert _outstanding(conn, "sales_invoice", si) == D("660.00")


def test_refuses_a_paid_invoice(conn, env):
    """The clearable-status rule is the shared lib's, not a second copy here."""
    si = _ar_invoice(conn, env, "1000.00")
    _receive(conn, env, "1000.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "1000.00"}])
    assert _doc_status(conn, "sales_invoice", si) == "paid"
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="1.00", account=env["bad_debt"])
    assert is_error(res) and "'paid'" in res["message"]
    assert _gl_legs(conn, "sales_invoice", si, "write_off") == []


def test_refuses_a_return(conn, env):
    si = _ar_invoice(conn, env, "1000.00")
    conn.execute("UPDATE sales_invoice SET is_return = 1 WHERE id = ?", (si,))
    conn.commit()
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="10.00", account=env["bad_debt"])
    assert is_error(res) and "return" in res["message"]


def test_refuses_missing_reason_and_bad_amounts(conn, env):
    si = _ar_invoice(conn, env, "1000.00")
    assert is_error(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                               amount="340.00", account=env["bad_debt"],
                               reason="   "))
    assert is_error(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                               amount="0", account=env["bad_debt"]))
    assert is_error(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                               amount="-5.00", account=env["bad_debt"]))
    assert is_error(_write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                               amount="not-money", account=env["bad_debt"]))
    assert _gl_legs(conn, "sales_invoice", si, "write_off") == []


def test_refuses_the_control_account_as_the_write_off_account(conn, env):
    """Charging the write-off to AR itself posts a self-cancelling pair."""
    si = _ar_invoice(conn, env, "1000.00")
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="340.00", account=env["ar"])
    assert is_error(res) and "must differ" in res["message"]
    assert _outstanding(conn, "sales_invoice", si) == D("1000.00")


def test_refuses_an_invoice_with_no_active_ledger_posting(conn, env):
    """INV-25's precondition: the summary may not move without the detail."""
    si = _ar_invoice(conn, env, "1000.00")
    conn.execute("UPDATE payment_ledger_entry SET delinked = 1 "
                 "WHERE voucher_type='sales_invoice' AND voucher_id = ?", (si,))
    conn.commit()
    res = _write_off(conn, voucher_type="sales_invoice", voucher_id=si,
                     amount="340.00", account=env["bad_debt"])
    assert is_error(res) and "no active payment ledger posting" in res["message"]
    assert _gl_legs(conn, "sales_invoice", si, "write_off") == []


def test_refuses_an_unknown_voucher_type(conn, env):
    res = _write_off(conn, voucher_type="payment_entry",
                     voucher_id=str(uuid.uuid4()), amount="1.00",
                     account=env["bad_debt"])
    assert is_error(res) and "--voucher-type" in res["message"]


def test_no_batch_entry_point_exists(conn):
    """N6 STRICT, guarded from the inside (plan §2.3 / §10 risk 2).

    F17 is in the floor by ruling N6a precisely because it is single-invoice.
    A plural flag or a second voucher-id argument grown onto this action is the
    boundary breached from the inside, so it is asserted rather than trusted.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(mod.write_off_invoice))
    fn = tree.body[0]
    # Prose is not a surface; strip the docstring so the "no batch write-off"
    # sentence in it cannot satisfy or fail this check.
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    body = ast.unparse(fn)
    for forbidden in ("voucher_ids", "invoice_ids", "batch", "for inv in",
                      "fetchall()"):
        assert forbidden not in body, f"batch surface appeared: {forbidden}"
    assert "write-off-invoice" in mod.ACTIONS
    assert not [a for a in mod.ACTIONS
                if "write-off" in a and a != "write-off-invoice"]


# ── PG lane (plan §4 F17 pin 6, item 1) ─────────────────────────────────────
#
# Sibling of the F17b lane in erpclaw-setup/tests. Two layers, because the live
# layer cannot run in CI: a dialect-free pin on what can be wrong statically —
# every raw SQL string this action issues must use the `?` placeholder the
# connection wrapper rewrites for psycopg2, and must use `decimal_sum` rather
# than a float-producing SUM — and a real live-server lane gated on
# ERPCLAW_PG_TEST_URL for the box leg.

def test_pg_the_write_off_raw_sql_is_dialect_portable():
    """Runs everywhere: the two portability rules this action must not break.

    `_active_write_off_gl` is the only hand-written SQL on the write-off path
    (everything else is PyPika or the shared lib). A `%s` placeholder or a bare
    SUM over TEXT money would work on SQLite and break, or silently lose
    precision, on PostgreSQL — the class of defect no SQLite-only suite sees.
    """
    import ast
    import inspect
    import re
    tree = ast.parse(inspect.getsource(mod._active_write_off_gl))
    fn = tree.body[0]
    # Drop the docstring: prose is not SQL and must not satisfy or fail this.
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    sql = " ".join(n.value for n in ast.walk(fn)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert "FROM gl_entry" in sql, "expected the SQL literal to be found"
    assert "%s" not in sql, "raw %s placeholder is not portable through the wrapper"
    assert sql.count("?") == 3, "voucher_type, voucher_id, entry_set stay bound"
    assert "decimal_sum(" in sql, "money must aggregate through decimal_sum"
    assert not re.search(r"\bSUM\s*\(", sql, re.IGNORECASE), \
        "a bare SUM over TEXT money loses precision on both dialects"


@pytest.mark.skipif(
    not os.environ.get("ERPCLAW_PG_TEST_URL"),
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg, plan §8.3)")
def test_pg_lane_write_off_invoice(monkeypatch):
    """The F17a pins re-run against a live PostgreSQL backend.

    DROPS the ``public`` schema — never point ERPCLAW_PG_TEST_URL at real data.
    Drives the REAL action through the real connection wrapper so placeholder
    rewriting, decimal_sum registration and the clearing lib are all exercised
    on the other dialect.
    """
    pg_url = os.environ["ERPCLAW_PG_TEST_URL"]
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", pg_url)

    import psycopg2
    setup = psycopg2.connect(pg_url)
    setup.autocommit = True
    with setup.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    setup.close()

    _spec_is = importlib.util.spec_from_file_location(
        "init_schema_f17_pg",
        os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "init_schema.py"))
    init_schema = importlib.util.module_from_spec(_spec_is)
    _spec_is.loader.exec_module(init_schema)
    init_schema.init_db(pg_url)

    sys.path.insert(0, os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib"))
    from erpclaw_lib.db import get_connection
    pg_conn = get_connection()
    try:
        env = build_ar_env(pg_conn)
        env["income"] = seed_account(pg_conn, env["company_id"], "Sales", "income")
        env["bad_debt"] = seed_account(pg_conn, env["company_id"],
                                       "Bad Debt Expense", "expense")
        si = _ar_invoice(pg_conn, env, "1000.00")

        res = _write_off(pg_conn, voucher_type="sales_invoice", voucher_id=si,
                         amount="340.00", account=env["bad_debt"])
        assert is_ok(res), res
        assert res["write_off_amount"] == "340.00"
        assert _outstanding(pg_conn, "sales_invoice", si) == D("660.00")
        assert _voucher_ple_net(pg_conn, "sales_invoice", si) == D("660.00")

        legs = _gl_legs(pg_conn, "sales_invoice", si, "write_off")
        assert len(legs) == 2
        assert sum(D(r["debit"]) for r in legs) == \
            sum(D(r["credit"]) for r in legs) == D("340.00")

        # The duplicate-write-off guard runs entirely inside the raw SQL above,
        # so it is the single most valuable thing to re-prove on this dialect.
        second = _write_off(pg_conn, voucher_type="sales_invoice", voucher_id=si,
                            amount="10.00", account=env["bad_debt"])
        assert is_error(second)
        assert "already carries a write-off of 340.00" in second["message"]

        _all_green(pg_conn, *_CORE)
    finally:
        pg_conn.close()
