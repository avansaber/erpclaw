"""Compare-and-set guards for payment edits and allocations.

`update-payment`, `allocate-payment` and `reconcile-payments` used to check a
status (and a residual) they read in Python and then write by id alone, so an
edit that committed after a concurrent submit changed a submitted payment
underneath its ledger rows, a cancelled payment could still clear an invoice,
and two allocations could each pass the residual check against the same stale
value. Each now takes the payment row with a guarded UPDATE before any other
payment, allocation or invoice row moves and decides from what it reads under
that guard. The interleaving is simulated by making the action's own first
read return a stale snapshot while the competitor's change is made through the
real actions and committed first.
"""
import json
from decimal import Decimal

import pytest

from payments_helpers import (
    build_ar_env, call_action, get_conn, is_error, is_ok, load_db_query, ns,
    seed_sales_invoice,
)

mod = load_db_query()

D = Decimal


@pytest.fixture
def env(conn):
    e = build_ar_env(conn)
    conn.commit()
    return e


def _receive(conn, env, amount, allocations=None, posting_date="2026-06-01",
             submit=True):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date=posting_date, party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    if submit:
        s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
        assert is_ok(s), s
    conn.commit()
    return pe_id


def _table_rows(conn, table):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM %s ORDER BY id" % table).fetchall()]


def _stale_fields_once(monkeypatch, **stale):
    real = mod._get_pe_or_err
    state = {"first": True}

    def _wrapper(conn_arg, pe_id_arg):
        row = real(conn_arg, pe_id_arg)
        if state["first"]:
            state["first"] = False
            row = dict(row)
            for key, value in stale.items():
                row[key] = value
        return row

    monkeypatch.setattr(mod, "_get_pe_or_err", _wrapper)


def test_update_of_a_payment_submitted_after_its_read_writes_nothing(
        conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "1000.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}], submit=False)
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(s), s
    conn.commit()

    before = {
        "pa": _table_rows(conn, "payment_allocation"),
        "gl": _table_rows(conn, "gl_entry"),
        "ple": _table_rows(conn, "payment_ledger_entry"),
        "audit": _table_rows(conn, "audit_log"),
    }

    _stale_fields_once(monkeypatch, status="draft")
    r = call_action(mod.update_payment, conn, ns(
        payment_entry_id=pe, paid_amount="500.00",
        reference_number=None, allocations=None))
    assert is_error(r), r
    assert r["message"] == "Cannot update: payment is 'submitted' (must be 'draft')"

    row = conn.execute(
        "SELECT paid_amount FROM payment_entry WHERE id = ?", (pe,)).fetchone()
    assert row["paid_amount"] == "1000.00"
    assert _table_rows(conn, "payment_allocation") == before["pa"]
    assert _table_rows(conn, "gl_entry") == before["gl"]
    assert _table_rows(conn, "payment_ledger_entry") == before["ple"]
    assert _table_rows(conn, "audit_log") == before["audit"]


def test_allocate_of_a_payment_cancelled_after_its_read_writes_nothing(
        conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "500.00", submit=True)
    c = call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(c), c
    conn.commit()

    pa_before = _table_rows(conn, "payment_allocation")
    gl_before = _table_rows(conn, "gl_entry")
    ple_before = _table_rows(conn, "payment_ledger_entry")

    _stale_fields_once(monkeypatch, status="submitted")
    r = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=pe, voucher_type="sales_invoice", voucher_id=si,
        allocated_amount="200.00"))
    assert is_error(r), r
    assert r["message"] == "Cannot allocate: payment is 'cancelled' (must be 'submitted')"

    row = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si,)).fetchone()
    assert row["outstanding_amount"] == "1000.00"
    assert _table_rows(conn, "payment_allocation") == pa_before
    assert _table_rows(conn, "gl_entry") == gl_before
    assert _table_rows(conn, "payment_ledger_entry") == ple_before


def test_second_allocation_against_a_stale_residual_is_refused(
        conn, env, monkeypatch):
    inv_a = seed_sales_invoice(conn, env, "1000.00")
    inv_b = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "500.00", submit=True)
    first = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=pe, voucher_type="sales_invoice", voucher_id=inv_a,
        allocated_amount="400.00"))
    assert is_ok(first), first
    conn.commit()

    _stale_fields_once(monkeypatch, unallocated_amount="500.00")
    r = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=pe, voucher_type="sales_invoice", voucher_id=inv_b,
        allocated_amount="400.00"))
    assert is_error(r), r
    assert r["message"] == "Allocated amount (400.00) exceeds unallocated (100.00)"

    row_b = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (inv_b,)).fetchone()
    assert row_b["outstanding_amount"] == "1000.00"
    row_pe = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe,)).fetchone()
    assert row_pe["unallocated_amount"] == "100.00"
    live = conn.execute(
        "SELECT COUNT(*) c FROM payment_allocation "
        "WHERE payment_entry_id = ? AND delinked = 0", (pe,)).fetchone()["c"]
    assert live == 1


class _RecordingProxy:
    def __init__(self, target):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "statements", [])

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_target"), name)

    def execute(self, sql, *params):
        self.statements.append(sql)
        return self._target.execute(sql, *params)

    def executemany(self, sql, *params):
        self.statements.append(sql)
        return self._target.executemany(sql, *params)


def _first_two_writes(statements):
    writes = [s for s in statements
              if s.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")
              or s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    return writes[:2]


def _write_kind(sql):
    return sql.lstrip().split(None, 1)[0].upper()


def test_allocate_takes_the_chain_head_before_the_payment_row(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "500.00", submit=True)
    conn.commit()

    proxy = _RecordingProxy(conn)
    r = call_action(mod.allocate_payment, proxy, ns(
        payment_entry_id=pe, voucher_type="sales_invoice", voucher_id=si,
        allocated_amount="200.00"))
    assert is_ok(r), r
    first_two = _first_two_writes(proxy.statements)
    assert len(first_two) >= 2, proxy.statements[:5]
    assert "gl_chain_head" in first_two[0]
    assert _write_kind(first_two[0]) == "INSERT"
    assert "payment_entry" in first_two[1]
    assert _write_kind(first_two[1]) == "UPDATE"


class _FrozenCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _CancelSeamProxy:
    """Run a competitor cancel inside the candidate SELECT.

    Forwards every attribute to the real connection. The FIRST execute call
    (the candidate SELECT) runs for real, then cancels P1 on a second
    connection and returns the pre-cancel rows, so the walk sees the stale
    candidate list while the guard sees the cancelled row.
    """

    def __init__(self, target, db_path, payment_to_cancel):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_db_path", db_path)
        object.__setattr__(self, "_payment_to_cancel", payment_to_cancel)
        object.__setattr__(self, "_calls", 0)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_target"), name)

    def execute(self, sql, *params):
        calls = object.__getattribute__(self, "_calls")
        object.__setattr__(self, "_calls", calls + 1)
        target = object.__getattribute__(self, "_target")
        if calls == 0:
            cursor = target.execute(sql, *params)
            rows = cursor.fetchall()
            other = get_conn(object.__getattribute__(self, "_db_path"))
            try:
                cancelled = call_action(
                    mod.cancel_payment, other, ns(
                        payment_entry_id=object.__getattribute__(
                            self, "_payment_to_cancel")))
                assert is_ok(cancelled), cancelled
            finally:
                other.close()
            return _FrozenCursor(rows)
        return target.execute(sql, *params)

    def executemany(self, sql, *params):
        target = object.__getattribute__(self, "_target")
        return target.executemany(sql, *params)


def test_reconcile_skips_a_payment_cancelled_after_the_candidate_read(
        conn, db_path):
    env = build_ar_env(conn)
    conn.commit()
    si = seed_sales_invoice(conn, env, "1000.00")
    p1 = _receive(conn, env, "300.00", posting_date="2026-06-01", submit=True)
    p2 = _receive(conn, env, "200.00", posting_date="2026-06-02", submit=True)
    conn.commit()
    p1_unallocated = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (p1,)).fetchone()["unallocated_amount"]

    proxy = _CancelSeamProxy(conn, db_path, p1)
    r = call_action(mod.reconcile_payments, proxy, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == [{"payment_id": p2, "voucher_id": si,
                             "allocated_amount": "200.00"}]
    assert r["unmatched_payments"] == 0
    assert r["unmatched_invoices"] == 1

    row = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si,)).fetchone()
    assert row["outstanding_amount"] == "800.00"
    live_p1 = conn.execute(
        "SELECT COUNT(*) c FROM payment_allocation "
        "WHERE payment_entry_id = ? AND delinked = 0", (p1,)).fetchone()["c"]
    assert live_p1 == 0
    row_p1 = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (p1,)).fetchone()
    assert row_p1["unallocated_amount"] == p1_unallocated


def test_uncontended_update_allocate_and_reconcile_unchanged(conn):
    env_a = build_ar_env(conn)
    conn.commit()
    si_a = seed_sales_invoice(conn, env_a, "1000.00")
    pe_a = _receive(conn, env_a, "1000.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si_a,
         "allocated_amount": "300.00"}], submit=False)
    r_a = call_action(mod.update_payment, conn, ns(
        payment_entry_id=pe_a, paid_amount="500.00",
        reference_number=None, allocations=None))
    assert is_ok(r_a), r_a
    assert r_a["updated_fields"] == ["paid_amount"]
    row_a = conn.execute(
        "SELECT paid_amount, unallocated_amount FROM payment_entry "
        "WHERE id = ?", (pe_a,)).fetchone()
    assert row_a["paid_amount"] == "500.00"
    assert row_a["unallocated_amount"] == "200.00"

    env_b = build_ar_env(conn)
    conn.commit()
    si_b = seed_sales_invoice(conn, env_b, "1000.00")
    pe_b = _receive(conn, env_b, "500.00", submit=True)
    r_b = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=pe_b, voucher_type="sales_invoice", voucher_id=si_b,
        allocated_amount="200.00"))
    assert is_ok(r_b), r_b
    assert r_b["document_cleared"] is True
    assert r_b["remaining_unallocated"] == "300.00"
    row_b = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_b,)).fetchone()
    assert row_b["outstanding_amount"] == "800.00"
    assert row_b["status"] == "partially_paid"

    env_c = build_ar_env(conn)
    conn.commit()
    si_c = seed_sales_invoice(conn, env_c, "1000.00")
    pe_c = _receive(conn, env_c, "300.00", submit=True)
    r_c = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env_c["customer"],
        company_id=env_c["company_id"]))
    assert is_ok(r_c), r_c
    assert r_c["matched"] == [{"payment_id": pe_c, "voucher_id": si_c,
                               "allocated_amount": "300.00"}]
    assert r_c["unmatched_payments"] == 0
    assert r_c["unmatched_invoices"] == 1
