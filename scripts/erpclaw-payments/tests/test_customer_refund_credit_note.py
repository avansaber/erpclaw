"""Customer refund clears a credit note (m660p).

A payment with payment_type 'pay' and party_type 'customer' is a customer
refund: it must be fully allocated to credit notes when added, clears each
credit note through the shared clearing lib, posts its party ledger with the
receivable sign (+paid_amount / +allocated), carries a sign-aware residual
compensation (-(allocated + deducted)), never routes to an advance account,
and never reconciles against an invoice.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

from payments_helpers import (build_ar_env, call_action, is_error, is_ok,
                              load_db_query, ns, seed_account,
                              seed_sales_invoice)

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


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
    _INV_PATH = os.path.join(_find_repo_root(_TESTS_DIR), "testing",
                             "invariant_engine.py")
except RuntimeError:
    _INV_PATH = ""
if _INV_PATH and os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_refund",
                                                   _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None

D = Decimal


def _inv25(conn):
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present")
    inv_engine._ensure_decimal_sum(conn)
    return inv_engine._check_inv25_ar_summary_detail(conn)


def _inv27(conn):
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present")
    inv_engine._ensure_decimal_sum(conn)
    return inv_engine._check_inv27_party_level_residual(conn)


def _fy_name(conn, company_id, posting_date):
    rows = conn.execute(
        "SELECT name FROM fiscal_year WHERE company_id = ? "
        "AND start_date <= ? AND end_date >= ?",
        (company_id, posting_date, posting_date)).fetchall()
    assert len(rows) == 1, f"expected one FY covering {posting_date}"
    return rows[0][0]


def _post_invoice_gl(conn, env, voucher_type, voucher_id, amount,
                     control, other, control_debit, posting_date):
    sys.path.insert(0, os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib"))
    from erpclaw_lib.gl_posting import insert_gl_entries
    fy = _fy_name(conn, env["company_id"], posting_date)
    control_leg = {"account_id": control,
                   "debit": str(amount) if control_debit else "0",
                   "credit": "0" if control_debit else str(amount),
                   "party_type": "customer", "party_id": env["customer"],
                   "fiscal_year": fy}
    other_leg = {"account_id": other,
                 "debit": "0" if control_debit else str(amount),
                 "credit": str(amount) if control_debit else "0",
                 "cost_center_id": env["cc"], "fiscal_year": fy}
    insert_gl_entries(conn, [control_leg, other_leg],
                      voucher_type=voucher_type, voucher_id=voucher_id,
                      posting_date=posting_date,
                      company_id=env["company_id"],
                      remarks=f"{voucher_type} {voucher_id}")
    conn.commit()


def _seed_credit_note(conn, env, return_against, amount="-9.25",
                      posting_date="2026-06-03"):
    cn_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, is_return, return_against, company_id) "
        "VALUES (?, ?, ?, ?, ?, '0', '0', ?, 'submitted', 1, ?, ?)",
        (cn_id, env["customer"], posting_date, amount, amount,
         amount, return_against, env["company_id"]))
    conn.execute(
        "INSERT INTO sales_invoice_item (id, sales_invoice_id, item_id, quantity, "
        " rate, amount, net_amount) VALUES (?, ?, 'ITEM-1', '1', ?, ?, ?)",
        (str(uuid.uuid4()), cn_id, amount, amount, amount))
    conn.execute(
        "INSERT INTO payment_ledger_entry "
        "(id, posting_date, account_id, party_type, party_id, voucher_type, "
        " voucher_id, against_voucher_type, against_voucher_id, amount, "
        " amount_in_account_currency, currency, delinked) "
        "VALUES (?, ?, ?, 'customer', ?, 'credit_note', ?, 'credit_note', ?, "
        " ?, ?, 'USD', 0)",
        (str(uuid.uuid4()), posting_date, env["ar"], env["customer"],
         cn_id, cn_id, amount, amount))
    conn.commit()
    _post_invoice_gl(conn, env, "credit_note", cn_id, "9.25",
                     control=env["ar"], other=env["income"],
                     control_debit=False, posting_date=posting_date)
    return cn_id


def _scenario_s(conn):
    env = build_ar_env(conn)
    env["income"] = seed_account(conn, env["company_id"], "Sales", "income")
    inv_a = seed_sales_invoice(conn, env, "18.50")
    _post_invoice_gl(conn, env, "sales_invoice", inv_a, "18.50",
                     control=env["ar"], other=env["income"],
                     control_debit=True, posting_date="2026-06-01")
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-02", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount="18.50",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "sales_invoice",
                                 "voucher_id": inv_a,
                                 "allocated_amount": "18.50"}]),
        deductions=None))
    assert is_ok(created), created
    s = call_action(mod.submit_payment, conn,
                    ns(payment_entry_id=created["payment_entry_id"]))
    assert is_ok(s), s
    cn1 = _seed_credit_note(conn, env, inv_a)
    return env, inv_a, created["payment_entry_id"], cn1


def _add_refund(conn, env, amount, cn_id, posting_date="2026-06-04",
                paid_to=None, allocations=None, deductions=None):
    if allocations is None:
        allocations = [{"voucher_type": "credit_note", "voucher_id": cn_id,
                        "allocated_amount": str(amount)}]
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date=posting_date, party_type="customer",
        party_id=env["customer"],
        paid_from_account=env["bank"],
        paid_to_account=paid_to or env["ar"],
        paid_amount=str(amount),
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=json.dumps(deductions) if deductions else None))
    return created


def _party_net(conn, party_id):
    net = D("0")
    for vt, amount, delinked in conn.execute(
            "SELECT voucher_type, amount, delinked FROM payment_ledger_entry "
            "WHERE party_type = 'customer' AND party_id = ?", (party_id,)):
        if vt == "payment_entry" or delinked == 0:
            net += D(amount)
    return net


def _gl_ar(conn, ar):
    net = D("0")
    for debit, credit in conn.execute(
            "SELECT debit, credit FROM gl_entry WHERE account_id = ? "
            "AND is_cancelled = 0", (ar,)):
        net += D(debit) - D(credit)
    return net


def _cn_state(conn, cn_id):
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (cn_id,)).fetchone()
    return row["outstanding_amount"], row["status"]


def _unallocated(conn, pe_id):
    return conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe_id,)).fetchone()[0]


def _pe_amounts(conn, pe_id):
    rows = conn.execute(
        "SELECT against_voucher_type, amount FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry' AND voucher_id = ?",
        (pe_id,)).fetchall()
    party_level = sorted([D(r["amount"]) for r in rows
                          if r["against_voucher_type"] is None])
    alloc = sorted([D(r["amount"]) for r in rows
                    if r["against_voucher_type"] == "credit_note"])
    comp = sorted([D(r["amount"]) for r in rows
                   if r["against_voucher_type"] == "payment_entry"])
    return party_level, alloc, comp


def test_refund_clears_credit_note_and_party_net_equals_gl(conn):
    env, inv_a, pe1, cn1 = _scenario_s(conn)
    created = _add_refund(conn, env, "9.25", cn1)
    assert is_ok(created), created
    rid = created["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid))
    assert is_ok(res), res
    assert res["documents_cleared"] == 1
    out, status = _cn_state(conn, cn1)
    assert out == "0"
    assert status == "paid"
    assert _unallocated(conn, rid) == "0.00"
    party_level, alloc, comp = _pe_amounts(conn, rid)
    assert party_level == [D("9.25")]
    assert alloc == [D("9.25")]
    assert comp == [D("-9.25")]
    assert _party_net(conn, env["customer"]) == D("0.00")
    assert _gl_ar(conn, env["ar"]) == D("0.00")
    assert _inv25(conn) is None
    assert _inv27(conn) is None


def test_unallocated_refund_refused(conn):
    env, inv_a, pe1, cn1 = _scenario_s(conn)
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date="2026-06-04", party_type="customer",
        party_id=env["customer"], paid_from_account=env["bank"],
        paid_to_account=env["ar"], paid_amount="9.25",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created), created
    rid = created["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid))
    assert is_error(res), res
    assert "must be fully allocated" in res.get("message", "")
    row = conn.execute(
        "SELECT status FROM payment_entry WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "draft"
    assert conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = 'payment_entry' "
        "AND voucher_id = ?", (rid,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry' AND voucher_id = ?",
        (rid,)).fetchone()[0] == 0


def _seed_reconcile_case(conn):
    env = build_ar_env(conn)
    env["income"] = seed_account(conn, env["company_id"], "Sales", "income")
    inv_b = seed_sales_invoice(conn, env, "40.00")
    refund_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO payment_entry (id, naming_series, payment_type, posting_date, "
        " party_type, party_id, paid_from_account, paid_to_account, paid_amount, "
        " received_amount, payment_currency, exchange_rate, status, "
        " unallocated_amount, company_id) "
        "VALUES (?, 'PAY-LEGACY', 'pay', '2026-06-05', 'customer', ?, ?, ?, "
        " '9.25', '9.25', 'USD', '1', 'submitted', '9.25', ?)",
        (refund_id, env["customer"], env["bank"], env["ar"],
         env["company_id"]))
    conn.commit()
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-10", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount="40.00",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created), created
    pe3 = created["payment_entry_id"]
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe3))
    assert is_ok(s), s
    return env, inv_b, refund_id, pe3


def test_reconcile_skips_refund(conn):
    env, inv_b, refund_id, pe3 = _seed_reconcile_case(conn)
    res = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(res), res
    assert len(res["matched"]) == 1
    assert res["matched"][0]["payment_id"] == pe3
    assert res["matched"][0]["voucher_id"] == inv_b
    assert res["matched"][0]["allocated_amount"] == "40.00"
    assert res["unmatched_payments"] == 0
    assert _unallocated(conn, refund_id) == "9.25"
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_allocation WHERE payment_entry_id = ?",
        (refund_id,)).fetchone()[0] == 0
    assert _unallocated(conn, pe3) == "0.00"
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (inv_b,)).fetchone()
    assert row["outstanding_amount"] == "0"
    assert row["status"] == "paid"


def test_unallocated_list_skips_refund(conn):
    env, inv_b, refund_id, pe3 = _seed_reconcile_case(conn)
    res = call_action(mod.get_unallocated_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(res), res
    assert [p["id"] for p in res["payments"]] == [pe3]


def test_s2_routing_ignores_refund(conn):
    env = build_ar_env(conn)
    env["income"] = seed_account(conn, env["company_id"], "Sales", "income")
    adv = seed_account(conn, env["company_id"], "Supplier Advance", "asset")
    ap = seed_account(conn, env["company_id"], "Creditors", "liability")
    conn.execute(
        "UPDATE company SET advance_to_supplier_account_id = ? WHERE id = ?",
        (adv, env["company_id"]))
    conn.commit()
    supp = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO supplier (id, name, supplier_type, status, company_id) "
        "VALUES (?, 'Gotham Steel', 'company', 'active', ?)",
        (supp, env["company_id"]))
    conn.commit()
    assert mod._resolve_advance_routing(conn, {
        "payment_type": "pay", "party_type": "customer",
        "company_id": env["company_id"],
        "paid_from_account": env["bank"],
        "paid_to_account": env["ar"]}) == (None, None, None)
    assert mod._resolve_advance_routing(conn, {
        "payment_type": "pay", "party_type": "supplier",
        "company_id": env["company_id"],
        "paid_from_account": env["bank"],
        "paid_to_account": ap}) == (adv, ap, "debit")
    inv_a = seed_sales_invoice(conn, env, "18.50")
    cn1 = _seed_credit_note(conn, env, inv_a)
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date="2026-06-04", party_type="customer",
        party_id=env["customer"], paid_from_account=env["bank"],
        paid_to_account=env["ar"], paid_amount="9.25",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created), created
    rid = created["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid))
    assert is_error(res), res
    assert "must be fully allocated" in res.get("message", "")
    assert conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = 'payment_entry' "
        "AND voucher_id = ?", (rid,)).fetchone()[0] == 0


def test_cancel_refund_restores_credit_note(conn):
    env, inv_a, pe1, cn1 = _scenario_s(conn)
    created = _add_refund(conn, env, "9.25", cn1)
    rid = created["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid))
    assert is_ok(res), res
    assert _cn_state(conn, cn1) == ("0", "paid")
    assert _party_net(conn, env["customer"]) == D("0.00")
    assert _gl_ar(conn, env["ar"]) == D("0.00")
    cres = call_action(mod.cancel_payment, conn, ns(payment_entry_id=rid))
    assert is_ok(cres), cres
    assert _cn_state(conn, cn1) == ("-9.25", "submitted")
    assert _gl_ar(conn, env["ar"]) == D("-9.25")
    assert _party_net(conn, env["customer"]) == D("-9.25")
    total = sum((D(r[0]) for r in conn.execute(
        "SELECT amount FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry' AND voucher_id = ?",
        (rid,))), D("0"))
    assert total == D("0.00")
    assert _inv25(conn) is None
    assert _inv27(conn) is None


def test_partial_refunds(conn):
    env, inv_a, pe1, cn1 = _scenario_s(conn)
    c1 = _add_refund(conn, env, "5.00", cn1)
    assert is_ok(c1), c1
    r1 = c1["payment_entry_id"]
    s1 = call_action(mod.submit_payment, conn, ns(payment_entry_id=r1))
    assert is_ok(s1), s1
    assert _cn_state(conn, cn1) == ("-4.25", "partially_paid")
    c2 = _add_refund(conn, env, "4.25", cn1)
    assert is_ok(c2), c2
    r2 = c2["payment_entry_id"]
    s2 = call_action(mod.submit_payment, conn, ns(payment_entry_id=r2))
    assert is_ok(s2), s2
    assert _cn_state(conn, cn1) == ("0", "paid")
    cc = call_action(mod.cancel_payment, conn, ns(payment_entry_id=r2))
    assert is_ok(cc), cc
    assert _cn_state(conn, cn1) == ("-4.25", "partially_paid")


def test_refund_refusals(conn):
    env, inv_a, pe1, cn1 = _scenario_s(conn)

    def _no_writes(rid):
        assert conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = 'payment_entry' "
            "AND voucher_id = ?", (rid,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM payment_ledger_entry "
            "WHERE voucher_type = 'payment_entry' AND voucher_id = ?",
            (rid,)).fetchone()[0] == 0

    r_inv = _add_refund(conn, env, "9.25", inv_a, allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": inv_a,
         "allocated_amount": "9.25"}])
    assert is_ok(r_inv), r_inv
    rid_inv = r_inv["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid_inv))
    assert is_error(res), res
    assert res["message"] == \
        "A customer refund can only be allocated to a credit note"
    assert conn.execute(
        "SELECT status FROM payment_entry WHERE id = ?",
        (rid_inv,)).fetchone()["status"] == "draft"
    _no_writes(rid_inv)

    r_ded = _add_refund(conn, env, "10.25", cn1, allocations=[
        {"voucher_type": "credit_note", "voucher_id": cn1,
         "allocated_amount": "9.25"}], deductions=[
        {"account_id": env["discount"], "amount": "1.00", "type": "other",
         "description": "t"}])
    assert is_ok(r_ded), r_ded
    rid_ded = r_ded["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid_ded))
    assert is_error(res), res
    assert res["message"] == "A customer refund cannot carry deductions"
    assert conn.execute(
        "SELECT status FROM payment_entry WHERE id = ?",
        (rid_ded,)).fetchone()["status"] == "draft"
    _no_writes(rid_ded)

    cust2 = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO customer (id, name, customer_type, status, company_id) "
        "VALUES (?, 'Wayne', 'company', 'active', ?)",
        (cust2, env["company_id"]))
    conn.commit()
    cn_other = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, is_return, return_against, company_id) "
        "VALUES (?, ?, '2026-06-03', '-9.25', '-9.25', '0', '0', '-9.25', "
        " 'submitted', 1, ?, ?)",
        (cn_other, cust2, inv_a, env["company_id"]))
    conn.execute(
        "INSERT INTO payment_ledger_entry "
        "(id, posting_date, account_id, party_type, party_id, voucher_type, "
        " voucher_id, against_voucher_type, against_voucher_id, amount, "
        " amount_in_account_currency, currency, delinked) "
        "VALUES (?, '2026-06-03', ?, 'customer', ?, 'credit_note', ?, "
        " 'credit_note', ?, '-9.25', '-9.25', 'USD', 0)",
        (str(uuid.uuid4()), env["ar"], cust2, cn_other, cn_other))
    conn.commit()
    r_other = _add_refund(conn, env, "9.25", cn_other)
    assert is_ok(r_other), r_other
    rid_other = r_other["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid_other))
    assert is_error(res), res
    assert res["message"] == f"Credit note {cn_other} belongs to another customer"
    assert conn.execute(
        "SELECT status FROM payment_entry WHERE id = ?",
        (rid_other,)).fetchone()["status"] == "draft"
    _no_writes(rid_other)

    ar2 = seed_account(conn, env["company_id"], "Debtors2", "asset")
    r_acct = _add_refund(conn, env, "9.25", cn1, paid_to=ar2)
    assert is_ok(r_acct), r_acct
    rid_acct = r_acct["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid_acct))
    assert is_error(res), res
    assert res["message"] == ("paid-to-account must be the receivable account "
                            f"of credit note {cn1}")
    assert conn.execute(
        "SELECT status FROM payment_entry WHERE id = ?",
        (rid_acct,)).fetchone()["status"] == "draft"
    _no_writes(rid_acct)

    r_big = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date="2026-06-04", party_type="customer",
        party_id=env["customer"], paid_from_account=env["bank"],
        paid_to_account=env["ar"], paid_amount="10.00",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "credit_note",
                                 "voucher_id": cn1,
                                 "allocated_amount": "10.00"}]),
        deductions=None))
    assert is_ok(r_big), r_big
    rid_big = r_big["payment_entry_id"]
    res = call_action(mod.submit_payment, conn, ns(payment_entry_id=rid_big))
    assert is_error(res), res
    assert "exceeds the open credit 9.25" in res.get("message", "")
    assert conn.execute(
        "SELECT status FROM payment_entry WHERE id = ?",
        (rid_big,)).fetchone()["status"] == "draft"
    _no_writes(rid_big)

    refund_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO payment_entry (id, naming_series, payment_type, posting_date, "
        " party_type, party_id, paid_from_account, paid_to_account, paid_amount, "
        " received_amount, payment_currency, exchange_rate, status, "
        " unallocated_amount, company_id) "
        "VALUES (?, 'PAY-LEGACY', 'pay', '2026-06-05', 'customer', ?, ?, ?, "
        " '9.25', '9.25', 'USD', '1', 'submitted', '9.25', ?)",
        (refund_id, env["customer"], env["bank"], env["ar"],
         env["company_id"]))
    conn.commit()
    res = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=refund_id, voucher_type="credit_note",
        voucher_id=cn1, allocated_amount="9.25"))
    assert is_error(res), res
    assert res["message"] == ("A customer refund is allocated when it is added; "
                            "allocate-payment does not apply to it")
    assert conn.execute(
        "SELECT status, unallocated_amount FROM payment_entry WHERE id = ?",
        (refund_id,)).fetchone()["unallocated_amount"] == "9.25"
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_allocation WHERE payment_entry_id = ?",
        (refund_id,)).fetchone()[0] == 0
