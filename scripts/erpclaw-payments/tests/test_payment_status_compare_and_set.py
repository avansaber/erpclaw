"""Compare-and-set lifecycle guards for erpclaw-payments.

Every lifecycle status change is a compare-and-set inside the action's own
transaction: the write matches only a row still in the status the action
read, and when it matches nothing the action rolls back and refuses with
the status the payment is in now.
"""
import json
from decimal import Decimal

import pytest

from payments_helpers import (
    build_ar_env, call_action, is_error, is_ok, load_db_query, ns,
    seed_sales_invoice,
)

mod = load_db_query()

D = Decimal


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


def _receive(conn, env, amount, allocations=None, deductions=None, submit=True,
             currency=None):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=currency,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=json.dumps(deductions) if deductions else None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    if submit:
        s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
        assert is_ok(s), s
    return pe_id


def _snap(conn, sql, params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _pe_rows(conn, pe_id):
    return _snap(conn, "SELECT * FROM payment_entry WHERE id = ?", (pe_id,))


def _pa_rows(conn, pe_id):
    return _snap(conn, "SELECT * FROM payment_allocation WHERE payment_entry_id = ? ORDER BY id", (pe_id,))


def _pd_rows(conn, pe_id):
    return _snap(conn, "SELECT * FROM payment_deduction WHERE payment_entry_id = ? ORDER BY id", (pe_id,))


def _gl_rows(conn, pe_id):
    return _snap(conn, "SELECT * FROM gl_entry WHERE voucher_type = 'payment_entry' AND voucher_id = ? ORDER BY id", (pe_id,))


def _ple_rows(conn, pe_id):
    return _snap(conn, "SELECT * FROM payment_ledger_entry WHERE voucher_type = 'payment_entry' AND voucher_id = ? ORDER BY id", (pe_id,))


def _audit_rows(conn, pe_id):
    return _snap(conn, "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (pe_id,))


def _status(conn, pe_id):
    row = conn.execute("SELECT status FROM payment_entry WHERE id = ?", (pe_id,)).fetchone()
    return row["status"] if row else None


def _stale_status_once(monkeypatch, stale_status):
    real = mod._get_pe_or_err
    state = {"first": True}

    def _wrapper(conn_arg, pe_id_arg):
        row = real(conn_arg, pe_id_arg)
        if state["first"]:
            state["first"] = False
            row = dict(row)
            row["status"] = stale_status
        return row

    monkeypatch.setattr(mod, "_get_pe_or_err", _wrapper)


def _stale_snapshot_once(monkeypatch, snapshot):
    real = mod._get_pe_or_err
    state = {"first": True}

    def _wrapper(conn_arg, pe_id_arg):
        if state["first"]:
            state["first"] = False
            return dict(snapshot)
        return real(conn_arg, pe_id_arg)

    monkeypatch.setattr(mod, "_get_pe_or_err", _wrapper)


def test_delete_refuses_a_payment_submitted_after_its_read(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}], submit=False)
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(s), s
    conn.commit()

    before = {
        "pe": _pe_rows(conn, pe),
        "pa": _pa_rows(conn, pe),
        "gl": _gl_rows(conn, pe),
        "ple": _ple_rows(conn, pe),
        "audit": _audit_rows(conn, pe),
    }

    _stale_status_once(monkeypatch, "draft")
    r = call_action(mod.delete_payment, conn, ns(payment_entry_id=pe))
    assert is_error(r), r
    assert r["message"] == "Cannot delete: payment is 'submitted' (only 'draft' can be deleted)"

    assert _pe_rows(conn, pe) == before["pe"]
    assert _pa_rows(conn, pe) == before["pa"]
    assert _gl_rows(conn, pe) == before["gl"]
    assert _ple_rows(conn, pe) == before["ple"]
    assert _audit_rows(conn, pe) == before["audit"]
    assert _status(conn, pe) == "submitted"


def test_delete_of_a_draft_still_deletes(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount="1000.00",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "sales_invoice",
                                  "voucher_id": si,
                                  "allocated_amount": "980.00"}]),
        deductions=json.dumps([{"account_id": env["discount"],
                                 "amount": "20.00",
                                 "type": "early_payment_discount"}])))
    assert is_ok(created), created
    pe = created["payment_entry_id"]
    audit_before = conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE action = 'delete-payment' AND entity_id = ?",
        (pe,)).fetchone()["c"]

    r = call_action(mod.delete_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(r), r

    assert conn.execute(
        "SELECT COUNT(*) c FROM payment_entry WHERE id = ?", (pe,)).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM payment_allocation WHERE payment_entry_id = ?", (pe,)).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM payment_deduction WHERE payment_entry_id = ?", (pe,)).fetchone()["c"] == 0
    audit_after = conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE action = 'delete-payment' AND entity_id = ?",
        (pe,)).fetchone()["c"]
    assert audit_after - audit_before == 1


def test_second_submit_after_its_read_writes_nothing(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    conn.commit()

    before = {
        "gl": _gl_rows(conn, pe),
        "ple": _ple_rows(conn, pe),
        "pa": _pa_rows(conn, pe),
        "audit": _audit_rows(conn, pe),
    }

    _stale_status_once(monkeypatch, "draft")
    r = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_error(r), r
    assert r["message"] == "Cannot submit: payment is 'submitted' (must be 'draft')"

    assert _gl_rows(conn, pe) == before["gl"]
    assert _ple_rows(conn, pe) == before["ple"]
    assert _pa_rows(conn, pe) == before["pa"]
    assert _audit_rows(conn, pe) == before["audit"]


def test_second_cancel_after_its_read_writes_nothing(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    c = call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(c), c
    conn.commit()

    before = {
        "gl": _gl_rows(conn, pe),
        "ple": _ple_rows(conn, pe),
    }

    _stale_status_once(monkeypatch, "submitted")
    r = call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe))
    assert is_error(r), r
    assert r["message"] == "Cannot cancel: payment is 'cancelled' (must be 'submitted')"

    assert _gl_rows(conn, pe) == before["gl"]
    assert _ple_rows(conn, pe) == before["ple"]


def test_submit_of_a_payment_deleted_after_its_read_writes_nothing(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}], submit=False)
    conn.commit()
    snapshot = dict(mod._get_pe_or_err(conn, pe))

    d = call_action(mod.delete_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(d), d
    conn.commit()

    _stale_snapshot_once(monkeypatch, snapshot)
    r = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_error(r), r
    assert r["message"] == f"Payment entry {pe} not found"

    assert conn.execute(
        "SELECT COUNT(*) c FROM gl_entry WHERE voucher_type = 'payment_entry' AND voucher_id = ?",
        (pe,)).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM payment_ledger_entry WHERE voucher_type = 'payment_entry' AND voucher_id = ?",
        (pe,)).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE action = 'submit-payment' AND entity_id = ?",
        (pe,)).fetchone()["c"] == 0


def test_uncontended_submit_and_cancel_unchanged(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}], submit=False)

    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(s), s

    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE voucher_type = 'payment_entry' AND voucher_id = ?",
        (pe,)).fetchall()
    assert rows, "submit must post GL legs"
    debit_total = sum((D(r["debit"]) for r in rows), D("0"))
    credit_total = sum((D(r["credit"]) for r in rows), D("0"))
    assert f"{debit_total:.2f}" == "300.00"
    assert f"{credit_total:.2f}" == "300.00"

    c = call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(c), c
    assert _status(conn, pe) == "cancelled"


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


def test_submit_and_cancel_take_the_chain_head_before_the_payment_row(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount="300.00",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "sales_invoice",
                                  "voucher_id": si,
                                  "allocated_amount": "300.00"}]),
        deductions=None))
    assert is_ok(created), created
    pe = created["payment_entry_id"]
    conn.commit()

    proxy = _RecordingProxy(conn)
    s = call_action(mod.submit_payment, proxy, ns(payment_entry_id=pe))
    assert is_ok(s), s
    first_two = _first_two_writes(proxy.statements)
    assert len(first_two) >= 2, proxy.statements[:5]
    assert "gl_chain_head" in first_two[0]
    assert _write_kind(first_two[0]) == "INSERT"
    assert "payment_entry" in first_two[1]
    assert _write_kind(first_two[1]) == "UPDATE"
    conn.commit()

    proxy2 = _RecordingProxy(conn)
    c = call_action(mod.cancel_payment, proxy2, ns(payment_entry_id=pe))
    assert is_ok(c), c
    first_two = _first_two_writes(proxy2.statements)
    assert len(first_two) >= 2, proxy2.statements[:5]
    assert "gl_chain_head" in first_two[0]
    assert _write_kind(first_two[0]) == "INSERT"
    assert "payment_entry" in first_two[1]
    assert _write_kind(first_two[1]) == "UPDATE"
