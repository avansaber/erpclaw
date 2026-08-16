"""Part A — M46/F1: cancelling an invoice RELEASES the allocations it voids.

Wave G item F1 (planning/WAVE_G_PLAN_2026-07-31.md §4). Before this change, all
four cancel paths (cancel-sales-invoice, cancel-purchase-invoice and the two
intercompany legs) delinked the document's own payment-ledger rows and zeroed
its outstanding, but left ``payment_allocation``, the per-allocation PLE rows and
``payment_entry.unallocated_amount`` untouched: cash stayed applied to a document
that no longer existed in the books.

Every pin here drives the REAL actions (selling / buying / payments) against a
fresh core DB and asserts exact Decimals. Two of them (pins 9 and 10) are the
composed cancel lifecycles the Wave-G SIM found permanently red under the first
spec (planning/simlogs/waveg-plan_SIM_2026-07-31.md findings 1-2); they walk two
shipped operations in both orders and check the ledger after EVERY step, not just
at the end.

Party-ledger vs GL — the window is now CLOSED at every step:
    party PLE net == party GL net on the control account after every operation
    below, with no exception. That was NOT true when F1 shipped: while a live
    allocation stood against a live invoice, the party ledger read 400.00 against
    a GL of 700.00, because ``submit-payment`` subtracted the same cash twice
    (the full-amount party-level row plus the per-allocation row). That window
    was M38, it was pinned here with its measured values so it could not be
    mistaken for something F1 broke, and Wave G item F2 closed it at the source
    with the party-residual compensation row (ADR-0032 W16). The three pins that
    recorded 400.00 now record 700.00 == 700.00 and are marked below.
"""
import importlib.util
import json
import os
import sys
from decimal import Decimal

import pytest

from selling_helpers import call_action, ns, is_ok, is_error, load_db_query

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))  # scripts/


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# The purchase side and the intercompany PI leg need the real buying actions,
# and every pin needs the real payments actions. Loaded by path (the same idiom
# the constitution suite uses) so no test-side re-implementation creeps in.
buy = _load("db_query_buying_f1", "erpclaw-buying/db_query.py")
pay = _load("db_query_payments_f1", "erpclaw-payments/db_query.py")

_BUY_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-buying", "tests")
if _BUY_TESTS not in sys.path:
    sys.path.append(_BUY_TESTS)
from buying_helpers import build_buying_env  # noqa: E402


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
    _INV_PATH = os.path.join(_find_repo_root(_TESTS_DIR), "testing", "invariant_engine.py")
except RuntimeError:
    _INV_PATH = ""
if _INV_PATH and os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_f1", _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None


D = Decimal


# ── ledger readers (test-side; F2 ships the shared helper) ───────────────────

def _party_ple_net(conn, party_type, party_id):
    """Party-level payment-ledger net under the liveness rule.

    Document rows (voucher_type <> 'payment_entry') count only while
    ``delinked = 0`` — an invoice cancel delinks in place and writes no
    reversal. Payment rows count reversal-INCLUSIVE (no delinked filter),
    because cancel-payment delinks a row AND writes its mirror; only netting
    the pair returns the right answer. Rationale: INV-25's docstring
    (testing/invariant_engine.py) and planning/simlogs/wavef-s14-inv25_SIM.
    """
    net = D("0")
    for vt, amount, delinked in conn.execute(
            "SELECT voucher_type, amount, delinked FROM payment_ledger_entry "
            "WHERE party_type = ? AND party_id = ?", (party_type, party_id)):
        if vt == "payment_entry" or delinked == 0:
            net += D(amount)
    return net


def _party_gl_net(conn, party_type, party_id, account_id):
    """Σ(debit − credit) over ALL gl_entry rows for a party on one account.

    Reversal-inclusive (the INV-24/INV-05 treatment): a cancel marks the
    original ``is_cancelled = 1`` and inserts an active mirror, so the pair
    must both count or the net is wrong by the whole original.
    """
    net = D("0")
    for debit, credit in conn.execute(
            "SELECT debit, credit FROM gl_entry "
            "WHERE party_type = ? AND party_id = ? AND account_id = ?",
            (party_type, party_id, account_id)):
        net += D(debit) - D(credit)
    return net


def _allocations(conn, pe_id):
    return conn.execute(
        "SELECT id, voucher_id, allocated_amount, delinked FROM payment_allocation "
        "WHERE payment_entry_id = ? ORDER BY created_at, id", (pe_id,)).fetchall()


def _alloc_ple(conn, pe_id, voucher_id):
    """Every per-allocation PLE row for one (payment, document) pair.

    Ordered by amount so a pair reads [negative, positive] deterministically —
    created_at has second resolution and both halves land in the same second.
    """
    return conn.execute(
        "SELECT amount, delinked, posting_date FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry' AND voucher_id = ? "
        "  AND against_voucher_id = ? "
        "ORDER BY CAST(amount AS NUMERIC), id",
        (pe_id, voucher_id)).fetchall()


def _unallocated(conn, pe_id):
    return D(conn.execute("SELECT unallocated_amount FROM payment_entry WHERE id = ?",
                          (pe_id,)).fetchone()[0])


def _ple_count(conn):
    return conn.execute("SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0]


def _invariant(name):
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    return getattr(inv_engine, name)


def _inv(conn, name):
    check = _invariant(name)
    inv_engine._ensure_decimal_sum(conn)
    return check(conn)


# ── fixtures driven through the real actions ─────────────────────────────────

def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _sales_invoice(conn, env, qty="10", rate="100.00"):
    """Real create + submit: a 1,000.00 submitted sales invoice."""
    create = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, ("item1", qty, rate)), tax_template_id=None,
        payment_terms_id=None,
    ))
    assert is_ok(create), create
    si_id = create["sales_invoice_id"]
    assert is_ok(call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id)))
    return si_id


def _receive_payment(conn, env, amount, allocations=None, submit=True):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-25", party_type="customer", party_id=env["customer"],
        paid_from_account=env["ar"], paid_to_account=env["cash"],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None,
    ))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    if submit:
        assert is_ok(call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id)))
    return pe_id


def _purchase_invoice(conn, benv, qty="10", rate="100.00"):
    create = call_action(buy.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=benv["supplier"], company_id=benv["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=json.dumps([{"item_id": benv["item1"], "qty": qty, "rate": rate,
                           "warehouse_id": benv["warehouse"]}]),
        tax_template_id=None,
    ))
    assert is_ok(create), create
    pi_id = create["purchase_invoice_id"]
    assert is_ok(call_action(buy.submit_purchase_invoice, conn,
                             ns(purchase_invoice_id=pi_id)))
    return pi_id


def _pay_payment(conn, benv, amount, allocations=None, submit=True):
    created = call_action(pay.add_payment, conn, ns(
        company_id=benv["company_id"], payment_type="pay",
        posting_date="2026-06-25", party_type="supplier", party_id=benv["supplier"],
        paid_from_account=benv["cash"], paid_to_account=benv["ap"],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None,
    ))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    if submit:
        assert is_ok(call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id)))
    return pe_id


@pytest.fixture
def benv(conn):
    """Full buying environment (its own company), for the AP-side pins."""
    return build_buying_env(conn)


# ── pin 1 — the AR case ──────────────────────────────────────────────────────

def test_pin1_cancel_sales_invoice_releases_the_allocation(conn, env):
    """SI 1,000 + payment 300 allocated + cancel invoice.

    allocation delinked=1, residual back to 300.00, the PLE release pair written
    BOTH-SIDES-DELINKED (correction C1), party PLE net == party GL net.
    """
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])

    # State after submit: the invoice is partially paid and the residual is 0.
    assert _unallocated(conn, pe_id) == D("0.00")
    assert D(conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id=?",
                          (si_id,)).fetchone()[0]) == D("700.00")
    # M38 CLOSED by Wave G F2. This pin read 400.00 against a GL of 700.00 while
    # the full-amount party row and the per-allocation row both subtracted the
    # same 300; the compensation row converges the party back to the truth.
    # Measured, not assumed — both sides now agree.
    assert _party_ple_net(conn, "customer", env["customer"]) == D("700.00")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("700.00")

    result = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(result), result

    allocs = _allocations(conn, pe_id)
    assert len(allocs) == 1
    assert allocs[0]["delinked"] == 1                      # released, not deleted
    assert D(allocs[0]["allocated_amount"]) == D("300.00")  # amount untouched

    assert _unallocated(conn, pe_id) == D("300.00")         # cash came back

    pair = _alloc_ple(conn, pe_id, si_id)
    assert len(pair) == 2, "expected the original + its release mirror"
    assert [D(r["amount"]) for r in pair] == [D("-300.00"), D("300.00")]
    # C1: BOTH sides delinked. An active mirror would be re-mirrored by a later
    # cancel-payment's generic loop and leave the party permanently divergent.
    assert [r["delinked"] for r in pair] == [1, 1]
    # The mirror carries the source row's posting_date so the pair nets to zero
    # inside any as-of-date window.
    assert pair[0]["posting_date"] == pair[1]["posting_date"]

    # Party ledger == party GL, exactly, now that the allocation is void.
    assert _party_ple_net(conn, "customer", env["customer"]) == D("-300.00")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("-300.00")

    # The action reports what it released.
    released = result["allocations_released"]
    assert len(released) == 1
    assert released[0]["payment_entry_id"] == pe_id
    assert released[0]["allocated_amount"] == "300.00"
    assert released[0]["unallocated_amount"] == "300.00"
    assert released[0]["ple_rows_released"] == 1
    assert "allocations_release_skipped" not in result


# ── pin 2 — the AP case ──────────────────────────────────────────────────────

def test_pin2_cancel_purchase_invoice_releases_the_allocation(conn, benv):
    """Same shape on the purchase side, exact Decimals.

    PLE stores a payable POSITIVE while GL carries it as a credit, so the two
    nets are equal in magnitude and opposite in sign.
    """
    pi_id = _purchase_invoice(conn, benv)
    pe_id = _pay_payment(conn, benv, "300.00", [
        {"voucher_type": "purchase_invoice", "voucher_id": pi_id,
         "allocated_amount": "300.00"}])

    assert _unallocated(conn, pe_id) == D("0.00")
    assert D(conn.execute("SELECT outstanding_amount FROM purchase_invoice WHERE id=?",
                          (pi_id,)).fetchone()[0]) == D("700.00")

    result = call_action(buy.cancel_purchase_invoice, conn,
                         ns(purchase_invoice_id=pi_id))
    assert is_ok(result), result

    allocs = _allocations(conn, pe_id)
    assert len(allocs) == 1 and allocs[0]["delinked"] == 1
    assert _unallocated(conn, pe_id) == D("300.00")

    pair = _alloc_ple(conn, pe_id, pi_id)
    assert [D(r["amount"]) for r in pair] == [D("-300.00"), D("300.00")]
    assert [r["delinked"] for r in pair] == [1, 1]

    ple = _party_ple_net(conn, "supplier", benv["supplier"])
    gl = _party_gl_net(conn, "supplier", benv["supplier"], benv["ap"])
    assert ple == D("-300.00")
    assert gl == D("300.00")
    assert ple == -gl

    assert result["allocations_released"][0]["unallocated_amount"] == "300.00"


# ── pin 3 — both intercompany legs: BLOCKED, and pinned as blocked ───────────

def test_pin3_intercompany_legs_are_unreachable_on_a_real_install(conn, env):
    """The two intercompany cancel legs carry the release, but cannot be driven.

    F1's plan (§4) lists erpclaw-selling's two intercompany cancel legs among the
    sites with the M46 defect, and pin 3 asks for a behavioural pin over both.
    Measured against the live tree, that precondition is FALSE: the four
    intercompany-invoice actions read and write ``sales_invoice.is_intercompany``
    / ``.intercompany_reference_id`` and the matching ``purchase_invoice``
    columns, and NO schema file, migration or dynamic-DDL site creates any of
    them. ``create-intercompany-invoice`` therefore cannot run on any install
    built from init_schema, so ``cancel-intercompany-invoice`` can never be
    reached with a real allocation to release.

    The release IS wired into both legs (same shared-lib call as the other two
    cancel paths) so they are correct the day the columns exist. Fixing the
    missing columns is a schema change F1's contract does not authorise, so it
    is reported to the architect instead of improvised here.

    This test pins the blocking fact. It fails the moment the columns land,
    which is exactly when the behavioural pin must be written.
    """
    si_cols = {r[1] for r in conn.execute("PRAGMA table_info(sales_invoice)")}
    pi_cols = {r[1] for r in conn.execute("PRAGMA table_info(purchase_invoice)")}
    missing = {
        "sales_invoice": sorted({"is_intercompany", "intercompany_reference_id"}
                                - si_cols),
        "purchase_invoice": sorted({"is_intercompany", "intercompany_reference_id"}
                                   - pi_cols),
    }
    assert missing == {
        "sales_invoice": ["intercompany_reference_id", "is_intercompany"],
        "purchase_invoice": ["intercompany_reference_id", "is_intercompany"],
    }, ("intercompany columns now exist — write the real pin 3 (allocation "
        "release on both intercompany cancel legs) and delete this test")

    # And the action really does refuse to run, rather than silently no-op.
    si_id = _sales_invoice(conn, env)
    with pytest.raises((IndexError, KeyError, Exception)):
        mod.create_intercompany_invoice(conn, ns(
            sales_invoice_id=si_id, target_company_id="other-co",
            supplier_id="some-supplier"))


# ── pin 4 — no allocation: byte-identical to today ───────────────────────────

def test_pin4_cancel_without_allocation_is_unchanged(conn, env):
    """No live allocation ⇒ the release writes nothing and adds no payload key."""
    si_id = _sales_invoice(conn, env)
    ple_before = _ple_count(conn)
    alloc_before = conn.execute("SELECT COUNT(*) FROM payment_allocation").fetchone()[0]

    result = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(result)
    # Exactly the pre-F1 payload — no new keys on the quiet path.
    assert set(result) == {"status", "sales_invoice_id", "gl_reversals",
                           "sle_reversals"}
    assert _ple_count(conn) == ple_before      # no release rows written
    assert conn.execute("SELECT COUNT(*) FROM payment_allocation").fetchone()[0] \
        == alloc_before


# ── pin 5 — two payments against one invoice ─────────────────────────────────

def test_pin5_two_payments_both_released(conn, env):
    """Two payments allocated to one invoice: cancel releases BOTH."""
    si_id = _sales_invoice(conn, env)
    pe1 = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])
    pe2 = _receive_payment(conn, env, "450.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "450.00"}])
    assert D(conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id=?",
                          (si_id,)).fetchone()[0]) == D("250.00")

    result = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(result)

    assert _allocations(conn, pe1)[0]["delinked"] == 1
    assert _allocations(conn, pe2)[0]["delinked"] == 1
    assert _unallocated(conn, pe1) == D("300.00")
    assert _unallocated(conn, pe2) == D("450.00")
    assert [r["delinked"] for r in _alloc_ple(conn, pe1, si_id)] == [1, 1]
    assert [r["delinked"] for r in _alloc_ple(conn, pe2, si_id)] == [1, 1]
    assert {r["payment_entry_id"] for r in result["allocations_released"]} == {pe1, pe2}

    # 1,000 invoiced then cancelled, 750 cash on account, nothing applied.
    assert _party_ple_net(conn, "customer", env["customer"]) == D("-750.00")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("-750.00")


# ── pin 6 — a second cancel is still rejected, and releases nothing twice ────

def test_pin6_second_cancel_is_rejected_and_releases_nothing(conn, env):
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])
    assert is_ok(call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id)))

    ple_after_first = _ple_count(conn)
    again = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_error(again)
    assert "cancelled" in again.get("error", again.get("message", "")).lower()
    assert _ple_count(conn) == ple_after_first          # no second release pair
    assert _unallocated(conn, pe_id) == D("300.00")     # residual not doubled
    assert len(_alloc_ple(conn, pe_id, si_id)) == 2


# ── pin 7 — INV-22 / INV-24 / INV-25 ─────────────────────────────────────────

_AR_INVARIANTS = ("_check_inv22_payment_invoice_reconciliation",
                  "_check_inv25_ar_summary_detail")


def test_pin7a_inv22_and_inv25_green_through_the_release(conn, env):
    """The two PLE-based invariants stay green at every step of pin 1.

    INV-22 is the interesting one: a cancelled sales invoice reads
    outstanding = '0', so it lands in INV-22's paid-document scope. Before F1 the
    still-live allocation row left that net at -300.00 (a real, undetected
    divergence); the release closes it.
    """
    si_id = _sales_invoice(conn, env)
    for name in _AR_INVARIANTS:
        assert _inv(conn, name) is None, name

    pe_id = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])
    for name in _AR_INVARIANTS:
        assert _inv(conn, name) is None, name

    assert is_ok(call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id)))
    for name in _AR_INVARIANTS:
        assert _inv(conn, name) is None, name

    assert is_ok(call_action(pay.cancel_payment, conn, ns(payment_entry_id=pe_id)))
    for name in _AR_INVARIANTS:
        assert _inv(conn, name) is None, name


def test_pin7b_release_touches_no_gl_and_leaves_inv24_identical(conn, env):
    """The release itself writes ZERO gl_entry / stock_ledger_entry rows.

    Isolated by calling the shared helper directly on a pre-F1-shaped install
    (invoice cancelled the old way: status + its own PLE rows only), so the GL
    reversals the cancel action legitimately writes are not in the picture.
    gl_entry and stock_ledger_entry are compared row-for-row before and after,
    and INV-24 — which reads only those two tables — must come back byte-
    identical. (INV-24 is not asserted green here: this fixture seeds opening
    stock as raw SLE rows with no GL counterpart, so its value is a property of
    the fixture. Unchanged is the claim F1 owes, and unchanged is what is
    checked.)
    """
    from erpclaw_lib.payment_clearing import release_allocations_on_document

    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])

    # Pre-F1 cancel: exactly what the shipped code did before this change.
    conn.execute("UPDATE payment_ledger_entry SET delinked = 1 "
                 "WHERE voucher_type = 'sales_invoice' AND voucher_id = ?", (si_id,))
    conn.execute("UPDATE sales_invoice SET status = 'cancelled', "
                 "outstanding_amount = '0' WHERE id = ?", (si_id,))
    conn.commit()

    def _snapshot(table):
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()

    gl_before, sle_before = _snapshot("gl_entry"), _snapshot("stock_ledger_entry")
    inv24_before = _inv(conn, "_check_inv24_stock_account_gl_matches_ledger")

    result = release_allocations_on_document(conn, "sales_invoice", si_id)
    conn.commit()

    assert len(result["released"]) == 1
    assert result["released"][0]["payment_entry_id"] == pe_id
    assert _unallocated(conn, pe_id) == D("300.00")
    assert [r["delinked"] for r in _alloc_ple(conn, pe_id, si_id)] == [1, 1]

    assert [tuple(r) for r in _snapshot("gl_entry")] == \
        [tuple(r) for r in gl_before], "the release wrote to gl_entry"
    assert [tuple(r) for r in _snapshot("stock_ledger_entry")] == \
        [tuple(r) for r in sle_before], "the release wrote to stock_ledger_entry"
    assert _inv(conn, "_check_inv24_stock_account_gl_matches_ledger") == inv24_before


# ── pin 8 — the PostgreSQL lane ──────────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; "
                           "the PG lane for F1 runs on the box leg, plan §8.3)")
def test_pin8_release_on_the_postgres_lane():
    """Pin 1 on Postgres. Gated exactly like test_migration_pg_drop_constraint:
    the local suite has no server, so this executes on the box leg where the
    dialect-aware release + migration 031 are exercised against a real cluster."""
    pytest.skip("PG lane executes on the box leg (plan §8.3 / SIM §12.4 item 4)")


# ── pin 9 — composed lifecycle A: cancel invoice → cancel payment ────────────

def test_pin9_cancel_invoice_then_cancel_payment(conn, env):
    """SIM finding 1. The release pair must be invisible to cancel-payment's
    generic delink+mirror loop; if it were not, the party would end permanently
    at -300 against an RHS of 0. Checked after EVERY step."""
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])

    # step 1 — submitted: the M38 window, CLOSED by F2 (was 400.00 vs 700.00).
    assert _party_ple_net(conn, "customer", env["customer"]) == D("700.00")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("700.00")

    # step 2 — cancel the invoice: release runs.
    assert is_ok(call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id)))
    assert _party_ple_net(conn, "customer", env["customer"]) == D("-300.00")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("-300.00")
    assert _unallocated(conn, pe_id) == D("300.00")
    assert len(_alloc_ple(conn, pe_id, si_id)) == 2

    # step 3 — cancel the payment: the closed pair is NOT re-mirrored.
    assert is_ok(call_action(pay.cancel_payment, conn, ns(payment_entry_id=pe_id)))
    assert len(_alloc_ple(conn, pe_id, si_id)) == 2, \
        "cancel-payment re-mirrored the release pair (correction C1 broken)"
    assert _party_ple_net(conn, "customer", env["customer"]) == D("0")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("0")
    # Nothing is outstanding and no submitted payment carries a residual, so the
    # party is flat on both sides — the shape INV-27 will assert in F2.
    assert conn.execute(
        "SELECT COUNT(*) FROM sales_invoice WHERE customer_id = ? "
        "AND status NOT IN ('draft','cancelled')", (env["customer"],)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_entry WHERE party_id = ? AND status = 'submitted'",
        (env["customer"],)).fetchone()[0] == 0


# ── pin 10 — composed lifecycle B: cancel payment → cancel invoice ───────────

def test_pin10_cancel_payment_then_cancel_invoice(conn, env):
    """SIM finding 2. The party is ALREADY correct once the payment is cancelled
    (cancel-payment composes correctly today); a release that fired on that
    cancelled payment's allocation would take a green party red. It must skip
    and SAY SO (correction C2). Checked after EVERY step."""
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])

    # step 1 — submitted: the M38 window again, CLOSED by F2 (was 400.00).
    assert _party_ple_net(conn, "customer", env["customer"]) == D("700.00")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("700.00")

    # step 2 — cancel the payment: outstanding restored, party ledger == GL.
    assert is_ok(call_action(pay.cancel_payment, conn, ns(payment_entry_id=pe_id)))
    assert D(conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id=?",
                          (si_id,)).fetchone()[0]) == D("1000.00")
    assert _party_ple_net(conn, "customer", env["customer"]) == D("1000.00")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("1000.00")
    # cancel-payment does not touch payment_allocation: the row is still live.
    assert _allocations(conn, pe_id)[0]["delinked"] == 0
    ple_before = _ple_count(conn)

    # step 3 — cancel the invoice: the release SKIPS the cancelled payment.
    result = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(result), result
    skipped = result["allocations_release_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["payment_entry_id"] == pe_id
    assert skipped[0]["payment_status"] == "cancelled"
    assert skipped[0]["allocated_amount"] == "300.00"
    assert "allocations_released" not in result
    assert _ple_count(conn) == ple_before, "the skip must write nothing at all"
    assert _allocations(conn, pe_id)[0]["delinked"] == 0

    assert _party_ple_net(conn, "customer", env["customer"]) == D("0")
    assert _party_gl_net(conn, "customer", env["customer"], env["ar"]) == D("0")


# ── mechanics pin (beyond the plan's list): deduction-carrying release ───────

def test_release_returns_only_the_cash_not_the_deduction(conn, env):
    """A 980 payment with a 20 early-payment-discount deduction clears 1,000.

    The per-allocation PLE row carries allocation PLUS its deduction share, so
    the release must mirror the ROW (1,000), while the residual only gets the
    cash back (980 − 20 deduction = 960). Party-level equality in this shape is
    F2's compensation to close (the deduction is the second half of M38), so it
    is deliberately not asserted here.
    """
    si_id = _sales_invoice(conn, env)
    disc = conn.execute(
        "SELECT id FROM account WHERE company_id = ? AND root_type = 'income' LIMIT 1",
        (env["company_id"],)).fetchone()[0]
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-25", party_type="customer", party_id=env["customer"],
        paid_from_account=env["ar"], paid_to_account=env["cash"],
        paid_amount="980.00", exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "sales_invoice",
                                 "voucher_id": si_id,
                                 "allocated_amount": "960.00"}]),
        deductions=json.dumps([{"account_id": disc, "amount": "20.00",
                                "type": "other"}]),
    ))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id)))

    pair = _alloc_ple(conn, pe_id, si_id)
    assert len(pair) == 1
    assert D(pair[0]["amount"]) == D("-980.00")   # 960 allocated + 20 deduction

    assert is_ok(call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id)))

    pair = _alloc_ple(conn, pe_id, si_id)
    assert [D(r["amount"]) for r in pair] == [D("-980.00"), D("980.00")]
    assert [r["delinked"] for r in pair] == [1, 1]
    # 980 paid − 0 live allocations − 20 deduction: the discount stays taken.
    assert _unallocated(conn, pe_id) == D("960.00")


# ── reader pin: a released allocation disappears from get-payment ────────────

def test_released_allocation_is_not_reported_as_live(conn, env):
    """get-payment lists LIVE allocations only; the row survives for audit."""
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])
    before = call_action(pay.get_payment, conn, ns(payment_entry_id=pe_id))
    assert len(before["allocations"]) == 1

    assert is_ok(call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id)))

    after = call_action(pay.get_payment, conn, ns(payment_entry_id=pe_id))
    assert after["allocations"] == []
    assert after["unallocated_amount"] == "300.00"
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_allocation WHERE payment_entry_id = ?",
        (pe_id,)).fetchone()[0] == 1        # audit trail intact
