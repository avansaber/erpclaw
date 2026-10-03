"""Strong-depth tests for 2 payments actions (m626-strong-depth-selling-payments-2).

Both actions route to one handler (get_unallocated_payments); each is driven
by its own action string with its own seeded values and its own read-back, so
the pair shares no assertion. Neither action had more than dispatch coverage
before this file; the ordered id sequences plus the exact hand-computed
unallocated strings below carry the weight.

Strong contract per action:
  1. Read back through the seam on a fresh connection from
     erpclaw_lib.db.get_connection(), comparing EXACT values.
  2. Hand-computed money literals (1000.00 - 340.00 - 150.25 = 509.75, and
     the same shape for every other residual: never copied from the output).
  3. A pinned refusal with the exact message and the database unchanged.
  4. Explicit NOT-changed assertions (neighbour payments, allocations, audit).

Conventions: money is TEXT; every monetary assertion compares exact strings
(never float). Test queries are built with PyPika through erpclaw_lib.query
and are parameterised. No raw catalog reads anywhere in this file.
"""
import json
import uuid

import pytest
from payments_helpers import (
    build_ap_env, build_ar_env, call_action, is_error, is_ok,
    load_db_query, ns, seed_sales_invoice,
)
from erpclaw_lib.db import get_connection as fresh_connection
from erpclaw_lib.query import Q, P, Table, fn, insert_row

pay = load_db_query()

T_PE = Table("payment_entry")
T_PA = Table("payment_allocation")
T_PLE = Table("payment_ledger_entry")
T_GL = Table("gl_entry")
T_SI = Table("sales_invoice")
T_AUDIT = Table("audit_log")
T_NS = Table("naming_series")


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


def _fresh():
    return fresh_connection()


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _count(conn, table):
    q = Q.from_(table).select(fn.Count("*"))
    return conn.execute(q.get_sql(), ()).fetchone()[0]


def _snapshot(conn, table):
    q = Q.from_(table).select(table.star).orderby(table.id)
    return [dict(r) for r in conn.execute(q.get_sql(), ()).fetchall()]


def _submitted_cross_company_same_party(conn, env_target, env_other,
                                        posting_date, paid_amount):
    """Submitted payment in the second company with the target's party.

    Inserted directly: the row stands for one written before add-payment
    checked its party (see
    TestAddPaymentPartyChecksStrong.test_cross_company_party_refused); it is
    the only decoy that reaches the company filter in
    get_unallocated_payments. No GL or payment-ledger rows are written."""
    pe_id = str(uuid.uuid4())
    sql, _ = insert_row("payment_entry", {
        "id": P(), "payment_type": P(), "posting_date": P(),
        "party_type": P(), "party_id": P(),
        "paid_from_account": P(), "paid_to_account": P(),
        "paid_amount": P(), "received_amount": P(),
        "unallocated_amount": P(), "status": P(), "company_id": P(),
    })
    conn.execute(sql, (pe_id, "receive", posting_date, "customer",
                       env_target["customer"], env_other["ar"],
                       env_other["bank"], paid_amount, paid_amount,
                       paid_amount, "submitted", env_other["company_id"]))
    conn.commit()
    return pe_id


def _submitted_supplier_type(conn, env, posting_date, paid_amount):
    """Submitted payment in the target company differing only in party_type.

    Inserted directly: the row stands for one written before add-payment
    checked its party (see
    TestAddPaymentPartyChecksStrong.test_party_type_mismatch_refused).
    No GL or payment-ledger rows are written."""
    pe_id = str(uuid.uuid4())
    sql, _ = insert_row("payment_entry", {
        "id": P(), "payment_type": P(), "posting_date": P(),
        "party_type": P(), "party_id": P(),
        "paid_from_account": P(), "paid_to_account": P(),
        "paid_amount": P(), "received_amount": P(),
        "unallocated_amount": P(), "status": P(), "company_id": P(),
    })
    conn.execute(sql, (pe_id, "receive", posting_date, "supplier",
                       env["customer"], env["ar"],
                       env["bank"], paid_amount, paid_amount,
                       paid_amount, "submitted", env["company_id"]))
    conn.commit()
    return pe_id


def _pe_by_id(conn, pe_id):
    q = Q.from_(T_PE).select(T_PE.star).where(T_PE.id == P())
    row = conn.execute(q.get_sql(), (pe_id,)).fetchone()
    return dict(row) if row else None


def _si_by_id(conn, si_id):
    q = Q.from_(T_SI).select(T_SI.star).where(T_SI.id == P())
    row = conn.execute(q.get_sql(), (si_id,)).fetchone()
    return dict(row) if row else None


def _second_customer(conn, company_id, name):
    sql, _ = insert_row("customer", {
        "id": P(), "name": P(), "customer_type": P(),
        "status": P(), "company_id": P(),
    })
    cid = str(uuid.uuid4())
    conn.execute(sql, (cid, name, "company", "active", company_id))
    conn.commit()
    return cid


def _submitted(conn, env, posting_date, paid_amount,
               customer=None, company=None, bank=None, ar=None):
    created = call_action(pay.add_payment, conn, ns(
        company_id=company or env["company_id"], payment_type="receive",
        posting_date=posting_date, party_type="customer",
        party_id=customer or env["customer"],
        paid_from_account=ar or env["ar"],
        paid_to_account=bank or env["bank"],
        paid_amount=paid_amount,
        exchange_rate=None, payment_currency=None,
        reference_number="WIRE-1", reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(call_action(pay.submit_payment, conn,
                             ns(payment_entry_id=pe_id)))
    return pe_id


def _allocate(conn, pe_id, amount, voucher_id):
    result = call_action(pay.allocate_payment, conn, ns(
        payment_entry_id=pe_id, voucher_type="advance",
        voucher_id=voucher_id, allocated_amount=amount))
    assert is_ok(result), result
    return result


def _draft(conn, env, posting_date, paid_amount):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date=posting_date, party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=paid_amount,
        exchange_rate=None, payment_currency=None,
        reference_number="WIRE-D", reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created), created
    return created["payment_entry_id"]


def _submitted_pay(conn, env, posting_date, paid_amount):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date=posting_date, party_type="supplier",
        party_id=env["supplier"],
        paid_from_account=env["bank"],
        paid_to_account=env["ap"],
        paid_amount=paid_amount,
        exchange_rate=None, payment_currency=None,
        reference_number="WIRE-P", reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(call_action(pay.submit_payment, conn,
                             ns(payment_entry_id=pe_id)))
    return pe_id


# ---------------------------------------------------------------------------
# 7. get-unallocated-payments
# ---------------------------------------------------------------------------

class TestGetUnallocatedPaymentsStrong:
    def test_filters_order_residuals_and_readback(self, conn, env):
        """The ordered id sequence plus the exact hand-computed residual
        strings below carry the weight."""
        other_cust = _second_customer(conn, env["company_id"], "Zed Corp")
        env_b = build_ar_env(conn)

        late = _submitted(conn, env, "2026-06-20", "1000.00")
        assert _allocate(conn, late, "340.00", "ADV-L1")["remaining_unallocated"] == "660.00"
        assert _allocate(conn, late, "150.25", "ADV-L2")["remaining_unallocated"] == "509.75"
        early = _submitted(conn, env, "2026-06-05", "750.00")
        assert _allocate(conn, early, "250.50", "ADV-E1")["remaining_unallocated"] == "499.50"

        draft = _draft(conn, env, "2026-06-01", "500.00")
        full = _submitted(conn, env, "2026-06-06", "400.00")
        assert _allocate(conn, full, "400.00", "ADV-F1")["remaining_unallocated"] == "0.00"
        other_party = _submitted(conn, env, "2026-06-07", "600.00",
                                 customer=other_cust)
        other_co = _submitted(conn, env_b, "2026-06-08", "900.00")
        same_party_other_co = _submitted_cross_company_same_party(
            conn, env, env_b, "2026-06-13", "625.00")
        supplier_type = _submitted_supplier_type(
            conn, env, "2026-06-14", "325.00")

        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ple_before = _snapshot(conn, T_PLE)
        gl_before = _snapshot(conn, T_GL)
        audit_before = _snapshot(conn, T_AUDIT)

        result = call_action(pay.get_unallocated_payments, conn, ns(
            party_type="customer", party_id=env["customer"],
            company_id=env["company_id"], company_name=None,
        ))
        assert is_ok(result)
        got_ids = [r["id"] for r in result["payments"]]
        assert got_ids == [early, late]
        for excluded in (draft, full, other_party, other_co,
                         same_party_other_co, supplier_type):
            assert excluded not in got_ids

        fresh = _fresh()
        try:
            by_id = {r["id"]: r for r in result["payments"]}
            assert by_id[late]["paid_amount"] == "1000.00"
            assert by_id[late]["unallocated_amount"] == "509.75"
            assert by_id[early]["paid_amount"] == "750.00"
            assert by_id[early]["unallocated_amount"] == "499.50"
            for pid in got_ids:
                stored = _pe_by_id(fresh, pid)
                out = by_id[pid]
                assert out["paid_amount"] == stored["paid_amount"]
                assert out["unallocated_amount"] == stored["unallocated_amount"]
                assert out["posting_date"] == stored["posting_date"]
                assert stored["status"] == "submitted"
                assert stored["company_id"] == env["company_id"]
                assert stored["party_id"] == env["customer"]
            assert _pe_by_id(fresh, draft)["status"] == "draft"
            assert _pe_by_id(fresh, full)["unallocated_amount"] == "0.00"
            stored = _pe_by_id(fresh, same_party_other_co)
            assert stored["status"] == "submitted"
            assert stored["company_id"] == env_b["company_id"]
            assert stored["party_type"] == "customer"
            assert stored["party_id"] == env["customer"]
            assert stored["unallocated_amount"] == "625.00"
            assert _pe_by_id(fresh, supplier_type)["party_type"] == "supplier"
        finally:
            _close(fresh)

        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_PLE) == ple_before
        assert _snapshot(conn, T_GL) == gl_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_missing_party_type_refusal_exact_and_unchanged(self, conn, env):
        """Pins the refusal with its exact message. The unchanged table counts
        below carry the weight."""
        target = _submitted(conn, env, "2026-06-10", "1000.00")
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ple_before = _snapshot(conn, T_PLE)
        gl_before = _snapshot(conn, T_GL)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(pay.get_unallocated_payments, conn, ns(
            party_type=None, party_id=env["customer"],
            company_id=env["company_id"], company_name=None,
        ))
        assert is_error(result)
        assert result["message"] == "--party-type is required"
        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_PLE) == ple_before
        assert _snapshot(conn, T_GL) == gl_before
        assert _snapshot(conn, T_AUDIT) == audit_before
        fresh = _fresh()
        try:
            assert _pe_by_id(fresh, target)["unallocated_amount"] == "1000.00"
        finally:
            _close(fresh)


# ---------------------------------------------------------------------------
# 8. list-open-advances
# ---------------------------------------------------------------------------

class TestListOpenAdvancesStrong:
    def test_own_seeds_filters_order_residuals_and_readback(self, conn, env):
        """Driven by the list-open-advances action string with seeds distinct
        from the get-unallocated-payments test above. The ordered id sequence
        plus the exact hand-computed residual strings below carry the weight."""
        assert pay.ACTIONS["list-open-advances"] is pay.get_unallocated_payments
        other_cust = _second_customer(conn, env["company_id"], "Amy Corp")
        env_b = build_ar_env(conn)

        big = _submitted(conn, env, "2026-06-12", "2000.00")
        assert _allocate(conn, big, "475.25", "ADV-B1")["remaining_unallocated"] == "1524.75"
        small = _submitted(conn, env, "2026-06-03", "300.00")

        draft = _draft(conn, env, "2026-06-02", "250.00")
        full = _submitted(conn, env, "2026-06-04", "120.00")
        assert _allocate(conn, full, "120.00", "ADV-Z1")["remaining_unallocated"] == "0.00"
        other_party = _submitted(conn, env, "2026-06-09", "450.00",
                                 customer=other_cust)
        other_co = _submitted(conn, env_b, "2026-06-11", "700.00")
        same_party_other_co = _submitted_cross_company_same_party(
            conn, env, env_b, "2026-06-13", "725.00")
        supplier_type = _submitted_supplier_type(
            conn, env, "2026-06-14", "375.00")

        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ple_before = _snapshot(conn, T_PLE)
        gl_before = _snapshot(conn, T_GL)
        audit_before = _snapshot(conn, T_AUDIT)

        handler = pay.ACTIONS["list-open-advances"]
        result = call_action(handler, conn, ns(
            party_type="customer", party_id=env["customer"],
            company_id=env["company_id"], company_name=None,
        ))
        assert is_ok(result)
        got_ids = [r["id"] for r in result["payments"]]
        assert got_ids == [small, big]
        for excluded in (draft, full, other_party, other_co,
                         same_party_other_co, supplier_type):
            assert excluded not in got_ids

        fresh = _fresh()
        try:
            by_id = {r["id"]: r for r in result["payments"]}
            assert by_id[big]["paid_amount"] == "2000.00"
            assert by_id[big]["unallocated_amount"] == "1524.75"
            assert by_id[small]["paid_amount"] == "300.00"
            assert by_id[small]["unallocated_amount"] == "300.00"
            for pid in got_ids:
                stored = _pe_by_id(fresh, pid)
                out = by_id[pid]
                assert out["paid_amount"] == stored["paid_amount"]
                assert out["unallocated_amount"] == stored["unallocated_amount"]
                assert out["posting_date"] == stored["posting_date"]
                assert stored["status"] == "submitted"
                assert stored["company_id"] == env["company_id"]
                assert stored["party_id"] == env["customer"]
            assert _pe_by_id(fresh, draft)["status"] == "draft"
            assert _pe_by_id(fresh, full)["unallocated_amount"] == "0.00"
            stored = _pe_by_id(fresh, same_party_other_co)
            assert stored["status"] == "submitted"
            assert stored["company_id"] == env_b["company_id"]
            assert stored["party_type"] == "customer"
            assert stored["party_id"] == env["customer"]
            assert stored["unallocated_amount"] == "725.00"
            assert _pe_by_id(fresh, supplier_type)["party_type"] == "supplier"
        finally:
            _close(fresh)

        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_PLE) == ple_before
        assert _snapshot(conn, T_GL) == gl_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_missing_party_id_refusal_exact_and_unchanged(self, conn, env):
        """Pins the refusal with its exact message. The unchanged table counts
        below carry the weight."""
        target = _submitted(conn, env, "2026-06-10", "800.00")
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ple_before = _snapshot(conn, T_PLE)
        gl_before = _snapshot(conn, T_GL)
        audit_before = _snapshot(conn, T_AUDIT)
        handler = pay.ACTIONS["list-open-advances"]
        result = call_action(handler, conn, ns(
            party_type="customer", party_id=None,
            company_id=env["company_id"], company_name=None,
        ))
        assert is_error(result)
        assert result["message"] == "--party-id is required"
        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_PLE) == ple_before
        assert _snapshot(conn, T_GL) == gl_before
        assert _snapshot(conn, T_AUDIT) == audit_before
        fresh = _fresh()
        try:
            assert _pe_by_id(fresh, target)["unallocated_amount"] == "800.00"
        finally:
            _close(fresh)


# ---------------------------------------------------------------------------
# 9. allocate-payment voucher checks
# ---------------------------------------------------------------------------

class TestAllocateVoucherChecksStrong:
    def test_missing_invoice_voucher_refused_and_nothing_written(self, conn, env):
        """Pins the corrected finding: for sales_invoice and purchase_invoice
        a missing voucher IS refused today — the clearing step raises "not
        found", the action rolls back and reports the exact message below.
        The whole-row snapshots pin that nothing was written."""
        pe_id = _submitted(conn, env, "2026-06-10", "1000.00")
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ple_before = _snapshot(conn, T_PLE)
        gl_before = _snapshot(conn, T_GL)
        audit_before = _snapshot(conn, T_AUDIT)

        missing_si = "SI-DOES-NOT-EXIST"
        result = call_action(pay.allocate_payment, conn, ns(
            payment_entry_id=pe_id, voucher_type="sales_invoice",
            voucher_id=missing_si, allocated_amount="100.00"))
        assert is_error(result)
        assert result["message"] == (
            f"Payment allocation failed: sales_invoice {missing_si} not found")

        missing_pi = "PI-DOES-NOT-EXIST"
        result = call_action(pay.allocate_payment, conn, ns(
            payment_entry_id=pe_id, voucher_type="purchase_invoice",
            voucher_id=missing_pi, allocated_amount="100.00"))
        assert is_error(result)
        assert result["message"] == (
            f"Payment allocation failed: purchase_invoice {missing_pi} not found")

        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_PLE) == ple_before
        assert _snapshot(conn, T_GL) == gl_before
        assert _snapshot(conn, T_AUDIT) == audit_before
        fresh = _fresh()
        try:
            assert _pe_by_id(fresh, pe_id)["unallocated_amount"] == "1000.00"
        finally:
            _close(fresh)

    def test_misspelt_voucher_type_refused(self, conn, env):
        """Asserts what should happen: allocating to a misspelt voucher type
        is refused, the payment's unallocated_amount is unchanged, the
        invoice's outstanding_amount is unchanged, and no allocation, ledger
        or audit row is written. Today the call succeeds and lowers the
        residual, so this fails."""
        pe_id = _submitted(conn, env, "2026-06-10", "1000.00")
        si_id = seed_sales_invoice(conn, env, "1000.00")
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ple_before = _snapshot(conn, T_PLE)
        gl_before = _snapshot(conn, T_GL)
        ns_before = _snapshot(conn, T_NS)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(pay.allocate_payment, conn, ns(
            payment_entry_id=pe_id, voucher_type="sales_invoce",
            voucher_id=si_id, allocated_amount="100.00"))
        assert is_error(result)
        assert result["message"] == (
            "Unknown voucher type 'sales_invoce' for a payment allocation")
        fresh = _fresh()
        try:
            assert _pe_by_id(fresh, pe_id)["unallocated_amount"] == "1000.00"
            assert _si_by_id(fresh, si_id)["outstanding_amount"] == "1000.00"
        finally:
            _close(fresh)
        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_PLE) == ple_before
        assert _snapshot(conn, T_GL) == gl_before
        assert _snapshot(conn, T_NS) == ns_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_add_payment_misspelt_allocation_refused(self, conn, env):
        """The same misspelling inside add-payment --allocations is refused
        with nothing written, including no naming_series step."""
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ns_before = _snapshot(conn, T_NS)
        audit_before = _snapshot(conn, T_AUDIT)
        allocs = json.dumps([{"voucher_type": "sales_invoce",
                              "voucher_id": "SI-X",
                              "allocated_amount": "100.00"}])
        result = call_action(pay.add_payment, conn, ns(
            company_id=env["company_id"], payment_type="receive",
            posting_date="2026-06-15", party_type="customer",
            party_id=env["customer"],
            paid_from_account=env["ar"],
            paid_to_account=env["bank"],
            paid_amount="1000.00",
            exchange_rate=None, payment_currency=None,
            reference_number="WIRE-AL", reference_date=None,
            allocations=allocs, deductions=None))
        assert is_error(result), result
        assert result["message"] == (
            "Unknown voucher type 'sales_invoce' for a payment allocation")
        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_NS) == ns_before
        assert _snapshot(conn, T_AUDIT) == audit_before
        fresh = _fresh()
        try:
            assert _snapshot(fresh, T_PE) == pe_before
            assert _snapshot(fresh, T_NS) == ns_before
        finally:
            _close(fresh)

    def test_advance_allocation_accepted(self, conn):
        """'advance' is a documented non-clearing type: allocating 250.50 of
        a 1000.00 supplier payment leaves 749.50 (guard)."""
        env_ap = build_ap_env(conn)
        pe_id = _submitted_pay(conn, env_ap, "2026-06-10", "1000.00")
        result = call_action(pay.allocate_payment, conn, ns(
            payment_entry_id=pe_id, voucher_type="advance",
            voucher_id="ADV-1", allocated_amount="250.50"))
        assert is_ok(result), result
        assert result["remaining_unallocated"] == "749.50"

    def test_update_payment_misspelt_allocation_refused(self, conn, env):
        """The same misspelling inside update-payment is refused; the draft
        keeps paid_amount 1000.00 and unallocated_amount 1000.00."""
        pe_id = _draft(conn, env, "2026-06-10", "1000.00")
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ns_before = _snapshot(conn, T_NS)
        audit_before = _snapshot(conn, T_AUDIT)
        allocs = json.dumps([{"voucher_type": "sales_invoce",
                              "voucher_id": "SI-X",
                              "allocated_amount": "100.00"}])
        result = call_action(pay.update_payment, conn, ns(
            payment_entry_id=pe_id, paid_amount="900.00",
            reference_number=None, allocations=allocs))
        assert is_error(result), result
        assert result["message"] == (
            "Unknown voucher type 'sales_invoce' for a payment allocation")
        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_NS) == ns_before
        assert _snapshot(conn, T_AUDIT) == audit_before
        fresh = _fresh()
        try:
            assert _pe_by_id(fresh, pe_id)["paid_amount"] == "1000.00"
            assert _pe_by_id(fresh, pe_id)["unallocated_amount"] == "1000.00"
        finally:
            _close(fresh)

    def test_on_account_allocation_accepted(self, conn):
        """'on_account' is a documented non-clearing type: after the advance
        step, allocating 100.00 leaves 649.50 (guard)."""
        env_ap = build_ap_env(conn)
        pe_id = _submitted_pay(conn, env_ap, "2026-06-10", "1000.00")
        first = call_action(pay.allocate_payment, conn, ns(
            payment_entry_id=pe_id, voucher_type="advance",
            voucher_id="ADV-1", allocated_amount="250.50"))
        assert is_ok(first), first
        assert first["remaining_unallocated"] == "749.50"
        result = call_action(pay.allocate_payment, conn, ns(
            payment_entry_id=pe_id, voucher_type="on_account",
            voucher_id="OA-1", allocated_amount="100.00"))
        assert is_ok(result), result
        assert result["remaining_unallocated"] == "649.50"


# ---------------------------------------------------------------------------
# 10. add-payment party checks (pinned expected failures)
# ---------------------------------------------------------------------------

class TestAddPaymentPartyChecksStrong:
    def test_cross_company_party_refused(self, conn, env):
        """Asserts what should happen: a payment naming another company's customer is refused with nothing written."""
        env_b = build_ar_env(conn)
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ns_before = _snapshot(conn, T_NS)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(pay.add_payment, conn, ns(
            company_id=env_b["company_id"], payment_type="receive",
            posting_date="2026-06-15", party_type="customer",
            party_id=env["customer"],
            paid_from_account=env_b["ar"],
            paid_to_account=env_b["bank"],
            paid_amount="500.00",
            exchange_rate=None, payment_currency=None,
            reference_number="WIRE-XC", reference_date=None,
            allocations=None, deductions=None))
        assert is_error(result), result
        assert result["message"] == (
            f"Customer {env['customer']} belongs to another company")
        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_NS) == ns_before
        assert _snapshot(conn, T_AUDIT) == audit_before
        fresh = _fresh()
        try:
            assert _snapshot(fresh, T_PE) == pe_before
            assert _snapshot(fresh, T_NS) == ns_before
            assert _snapshot(fresh, T_AUDIT) == audit_before
        finally:
            _close(fresh)

    def test_party_type_mismatch_refused(self, conn, env):
        """Asserts what should happen: a payment naming a customer id under party_type supplier is refused with nothing written."""
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ns_before = _snapshot(conn, T_NS)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(pay.add_payment, conn, ns(
            company_id=env["company_id"], payment_type="receive",
            posting_date="2026-06-15", party_type="supplier",
            party_id=env["customer"],
            paid_from_account=env["ar"],
            paid_to_account=env["bank"],
            paid_amount="500.00",
            exchange_rate=None, payment_currency=None,
            reference_number="WIRE-SM", reference_date=None,
            allocations=None, deductions=None))
        assert is_error(result), result
        assert result["message"] == (
            f"Supplier {env['customer']} not found")
        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_NS) == ns_before
        assert _snapshot(conn, T_AUDIT) == audit_before
        fresh = _fresh()
        try:
            assert _snapshot(fresh, T_PE) == pe_before
            assert _snapshot(fresh, T_NS) == ns_before
            assert _snapshot(fresh, T_AUDIT) == audit_before
        finally:
            _close(fresh)

    def test_unknown_party_refused(self, conn, env):
        """A payment naming a party id that exists in no table is refused
        with nothing written."""
        pe_before = _snapshot(conn, T_PE)
        pa_before = _snapshot(conn, T_PA)
        ns_before = _snapshot(conn, T_NS)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(pay.add_payment, conn, ns(
            company_id=env["company_id"], payment_type="receive",
            posting_date="2026-06-15", party_type="customer",
            party_id="ghost-party",
            paid_from_account=env["ar"],
            paid_to_account=env["bank"],
            paid_amount="500.00",
            exchange_rate=None, payment_currency=None,
            reference_number="WIRE-GH", reference_date=None,
            allocations=None, deductions=None))
        assert is_error(result), result
        assert result["message"] == "Customer ghost-party not found"
        assert _snapshot(conn, T_PE) == pe_before
        assert _snapshot(conn, T_PA) == pa_before
        assert _snapshot(conn, T_NS) == ns_before
        assert _snapshot(conn, T_AUDIT) == audit_before
        fresh = _fresh()
        try:
            assert _snapshot(fresh, T_PE) == pe_before
            assert _snapshot(fresh, T_NS) == ns_before
            assert _snapshot(fresh, T_AUDIT) == audit_before
        finally:
            _close(fresh)

    def test_supplier_party_accepted(self, conn):
        """A payment naming the company's own supplier is accepted, holding
        paid_amount 300.00 and unallocated_amount 300.00 (guard)."""
        env_ap = build_ap_env(conn)
        result = call_action(pay.add_payment, conn, ns(
            company_id=env_ap["company_id"], payment_type="pay",
            posting_date="2026-06-15", party_type="supplier",
            party_id=env_ap["supplier"],
            paid_from_account=env_ap["bank"],
            paid_to_account=env_ap["ap"],
            paid_amount="300.00",
            exchange_rate=None, payment_currency=None,
            reference_number="WIRE-AP", reference_date=None,
            allocations=None, deductions=None))
        assert is_ok(result), result
        fresh = _fresh()
        try:
            stored = _pe_by_id(fresh, result["payment_entry_id"])
            assert stored["paid_amount"] == "300.00"
            assert stored["unallocated_amount"] == "300.00"
        finally:
            _close(fresh)
