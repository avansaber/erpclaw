"""reconcile-payments offers each invoice at what it still owes (m777)."""
import json
import uuid
from decimal import Decimal

from payments_helpers import (build_ap_env, build_ar_env, call_action, is_ok,
                              load_db_query, ns, seed_purchase_invoice,
                              seed_sales_invoice)

mod = load_db_query()

D = Decimal


def _receive(conn, env, amount, allocations=None):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def _pay(conn, env, amount, allocations=None):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date="2026-06-01", party_type="supplier",
        party_id=env["supplier"], paid_from_account=env["bank"],
        paid_to_account=env["ap"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def test_partly_paid_receivable_offered_at_remaining(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000.00")
    _receive(conn, env, "400.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "400.00"}])
    second = _receive(conn, env, "1000.00")
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == [{"payment_id": second, "voucher_id": si,
                             "allocated_amount": "600.00"}]
    assert r["unmatched_payments"] == 1
    assert r["unmatched_invoices"] == 0
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si,)).fetchone()
    assert row["outstanding_amount"] == "0"
    assert row["status"] == "paid"
    assert conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (second,)).fetchone()[0] == "400.00"
    rows = conn.execute(
        "SELECT allocated_amount FROM payment_allocation "
        "WHERE payment_entry_id = ?", (second,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["allocated_amount"] == "600.00"


def test_partly_paid_payable_offered_at_remaining(conn):
    env = build_ap_env(conn)
    pi = seed_purchase_invoice(conn, env, "1000.00")
    _pay(conn, env, "300.00", allocations=[
        {"voucher_type": "purchase_invoice", "voucher_id": pi,
         "allocated_amount": "300.00"}])
    second = _pay(conn, env, "1000.00")
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="supplier", party_id=env["supplier"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == [{"payment_id": second, "voucher_id": pi,
                             "allocated_amount": "700.00"}]
    row = conn.execute(
        "SELECT outstanding_amount, status FROM purchase_invoice WHERE id = ?",
        (pi,)).fetchone()
    assert row["outstanding_amount"] == "0"
    assert row["status"] == "paid"
    assert conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (second,)).fetchone()[0] == "300.00"


def test_paid_invoice_is_not_a_candidate(conn):
    env = build_ar_env(conn)
    inv_a = seed_sales_invoice(conn, env, "500.00")
    _receive(conn, env, "500.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": inv_a,
         "allocated_amount": "500.00"}])
    inv_b = seed_sales_invoice(conn, env, "300.00")
    conn.execute("UPDATE sales_invoice SET posting_date = '2026-06-15' "
                 "WHERE id = ?", (inv_b,))
    conn.execute("UPDATE payment_ledger_entry SET posting_date = '2026-06-15' "
                 "WHERE voucher_type = 'sales_invoice' AND voucher_id = ?",
                 (inv_b,))
    conn.commit()
    pe = _receive(conn, env, "300.00")
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == [{"payment_id": pe, "voucher_id": inv_b,
                             "allocated_amount": "300.00"}]
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (inv_a,)).fetchone()
    assert row["outstanding_amount"] == "0"
    assert row["status"] == "paid"


def test_other_company_invoice_is_not_a_candidate(conn):
    env = build_ar_env(conn)
    other = str(uuid.uuid4())
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (other, "Other Co %s" % other[:6], "OC%s" % other[:4]))
    conn.commit()
    other_si = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, company_id) "
        "VALUES (?, ?, '2026-06-01', '250.00', '250.00', '0', '0', '250.00', "
        " 'submitted', ?)",
        (other_si, env["customer"], other))
    conn.execute(
        "INSERT INTO sales_invoice_item (id, sales_invoice_id, item_id, quantity, "
        " rate, amount, net_amount) VALUES (?, ?, 'ITEM-1', '1', '250.00', "
        " '250.00', '250.00')", (str(uuid.uuid4()), other_si))
    conn.execute(
        "INSERT INTO payment_ledger_entry "
        "(id, posting_date, account_id, party_type, party_id, voucher_type, "
        " voucher_id, amount, amount_in_account_currency, currency, delinked) "
        "VALUES (?, '2026-06-01', ?, 'customer', ?, 'sales_invoice', ?, "
        " '250.00', '250.00', 'USD', 0)",
        (str(uuid.uuid4()), env["ar"], env["customer"], other_si))
    conn.execute("UPDATE sales_invoice SET posting_date = '2026-05-01' "
                 "WHERE id = ?", (other_si,))
    conn.execute("UPDATE payment_ledger_entry SET posting_date = '2026-05-01' "
                 "WHERE voucher_type = 'sales_invoice' AND voucher_id = ?",
                 (other_si,))
    conn.commit()
    home_si = seed_sales_invoice(conn, env, "250.00")
    pe = _receive(conn, env, "250.00")
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == [{"payment_id": pe, "voucher_id": home_si,
                             "allocated_amount": "250.00"}]
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (other_si,)).fetchone()
    assert row["outstanding_amount"] == "250.00"
    assert row["status"] == "submitted"


def test_credit_noted_invoice_offered_at_column(conn):
    env = build_ar_env(conn)
    inv_o = seed_sales_invoice(conn, env, "1000.00")
    conn.execute("UPDATE sales_invoice SET outstanding_amount = '800.00', "
                 "status = 'partially_paid' WHERE id = ?", (inv_o,))
    conn.commit()
    cn_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, is_return, return_against, company_id) "
        "VALUES (?, ?, '2026-06-01', '-200.00', '-200.00', '0', '0', '0', "
        " 'submitted', 1, ?, ?)",
        (cn_id, env["customer"], inv_o, env["company_id"]))
    conn.execute(
        "INSERT INTO sales_invoice_item (id, sales_invoice_id, item_id, quantity, "
        " rate, amount, net_amount) VALUES (?, ?, 'ITEM-1', '1', '-200.00', "
        " '-200.00', '-200.00')", (str(uuid.uuid4()), cn_id))
    for amount in ("-200.00", "200.00"):
        conn.execute(
            "INSERT INTO payment_ledger_entry "
            "(id, posting_date, account_id, party_type, party_id, voucher_type, "
            " voucher_id, against_voucher_type, against_voucher_id, amount, "
            " amount_in_account_currency, currency, delinked) "
            "VALUES (?, '2026-06-01', ?, 'customer', ?, 'credit_note', ?, "
            " 'credit_note', ?, ?, ?, 'USD', 0)",
            (str(uuid.uuid4()), env["ar"], env["customer"], cn_id, cn_id,
             amount, amount))
    conn.execute(
        "INSERT INTO payment_ledger_entry "
        "(id, posting_date, account_id, party_type, party_id, voucher_type, "
        " voucher_id, against_voucher_type, against_voucher_id, amount, "
        " amount_in_account_currency, currency, delinked) "
        "VALUES (?, '2026-06-01', ?, 'customer', ?, 'credit_note', ?, "
        " 'sales_invoice', ?, '-200.00', '-200.00', 'USD', 0)",
        (str(uuid.uuid4()), env["ar"], env["customer"], cn_id, inv_o))
    conn.commit()
    pe = _receive(conn, env, "1000.00")
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == [{"payment_id": pe, "voucher_id": inv_o,
                             "allocated_amount": "800.00"}]
    assert r["unmatched_payments"] == 1
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (inv_o,)).fetchone()
    assert row["outstanding_amount"] == "0"
    assert row["status"] == "paid"
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (cn_id,)).fetchone()
    assert row["outstanding_amount"] == "0"
    assert row["status"] == "submitted"
    assert conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe,)).fetchone()[0] == "200.00"


def test_credit_note_unabsorbed_shape_never_a_candidate(conn):
    env = build_ar_env(conn)
    inv_o = seed_sales_invoice(conn, env, "1000.00")
    cn_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, is_return, return_against, company_id) "
        "VALUES (?, ?, '2026-06-01', '-200.00', '-200.00', '0', '0', '-200.00', "
        " 'submitted', 1, ?, ?)",
        (cn_id, env["customer"], inv_o, env["company_id"]))
    conn.execute(
        "INSERT INTO sales_invoice_item (id, sales_invoice_id, item_id, quantity, "
        " rate, amount, net_amount) VALUES (?, ?, 'ITEM-1', '1', '-200.00', "
        " '-200.00', '-200.00')", (str(uuid.uuid4()), cn_id))
    conn.execute(
        "INSERT INTO payment_ledger_entry "
        "(id, posting_date, account_id, party_type, party_id, voucher_type, "
        " voucher_id, against_voucher_type, against_voucher_id, amount, "
        " amount_in_account_currency, currency, delinked) "
        "VALUES (?, '2026-06-01', ?, 'customer', ?, 'credit_note', ?, "
        " 'sales_invoice', ?, '-200.00', '-200.00', 'USD', 0)",
        (str(uuid.uuid4()), env["ar"], env["customer"], cn_id, inv_o))
    conn.commit()
    pe = _receive(conn, env, "1000.00")
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == [{"payment_id": pe, "voucher_id": inv_o,
                             "allocated_amount": "1000.00"}]


def test_unsupported_party_type_matches_nothing(conn):
    env = build_ar_env(conn)
    before_pa = conn.execute(
        "SELECT COUNT(*) FROM payment_allocation").fetchone()[0]
    before_ple = conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0]
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="employee", party_id="EMP-1",
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == []
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_allocation").fetchone()[0] == before_pa
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0] == before_ple


def test_candidate_statuses_equal_clearable_statuses():
    from erpclaw_lib import payment_clearing
    assert (mod._RECONCILE_CANDIDATE_STATUSES
            == payment_clearing._CLEARABLE_STATUSES)
