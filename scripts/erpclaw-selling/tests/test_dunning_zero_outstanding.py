"""run-dunning-cycle selects overdue invoices by the numeric value of their
outstanding amount.

outstanding_amount is TEXT. A fully discounted invoice, created and submitted
through the module's own actions, is stored as status 'submitted' with
outstanding "0.00". Compared as text, "0.00" > "0" is true, so such an invoice
would be dunned, and an old one would escalate its customer to a hold level.
The comparison must be numeric: only invoices that still owe money are chosen.

Every invoice here is created and submitted with create-sales-invoice and
submit-sales-invoice, with fixed posting and due dates, and the cycle runs with
a fixed --run-date, so the outcome does not depend on today's date.
"""
import importlib.util
import json
import os
import uuid

from selling_helpers import (call_action, is_error, is_ok, load_db_query,
                             ns, seed_customer)
from erpclaw_lib.query import Q, P, Table

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))  # scripts/


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pay = _load("db_query_payments_dunning", "erpclaw-payments/db_query.py")

_t_sales_invoice = Table("sales_invoice")


def _receive_payment(conn, env, amount, posting_date, allocations):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date=posting_date, party_type="customer",
        party_id=env["customer"],
        paid_from_account=env["ar"], paid_to_account=env["cash"],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations),
        deductions=None,
    ))
    assert is_ok(created), created
    submitted = call_action(pay.submit_payment, conn, ns(
        payment_entry_id=created["payment_entry_id"],
    ))
    assert is_ok(submitted), submitted
    return created["payment_entry_id"]

RUN_DATE = "2026-06-02"
NO_ACTIONS = {"email": 0, "hold": 0, "call": 0, "suspend": 0, "skipped": 0}


def _level(conn, company_id, level, days_overdue, action):
    conn.execute(
        "INSERT INTO dunning_level (id, company_id, level, days_overdue, action, template_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), company_id, level, days_overdue, action, None),
    )
    conn.commit()


def _submitted_invoice(conn, env, customer_id, qty, rate, discount, due_date):
    items = json.dumps([{
        "item_id": env["item1"], "qty": qty, "rate": rate,
        "discount_percentage": discount, "warehouse_id": env["warehouse"],
    }])
    created = call_action(mod.create_sales_invoice, conn, ns(
        company_id=env["company_id"], customer_id=customer_id, items=items,
        posting_date="2026-02-02", due_date=due_date, tax_template_id=None,
        sales_order_id=None, delivery_note_id=None, payment_terms_id=None))
    assert is_ok(created), created
    submitted = call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=created["sales_invoice_id"]))
    assert is_ok(submitted), submitted
    return created["sales_invoice_id"]


def _doc(conn, invoice_id):
    row = conn.execute(
        "SELECT status, grand_total, outstanding_amount FROM sales_invoice WHERE id = ?",
        (invoice_id,)).fetchone()
    return (row["status"], row["grand_total"], row["outstanding_amount"])


def _credit_status(conn, customer_id):
    return conn.execute(
        "SELECT credit_status FROM customer WHERE id = ?", (customer_id,)
    ).fetchone()["credit_status"]


def _runs(conn, company_id):
    return conn.execute(
        "SELECT customer_id, level, action_taken, status, run_date, notes, invoice_ids_json "
        "FROM dunning_run WHERE company_id = ?", (company_id,)).fetchall()


def _run_cycle(conn, company_id):
    return call_action(mod.run_dunning_cycle, conn, ns(
        company_id=company_id, run_date=RUN_DATE, db_path=None))


def test_only_invoices_that_still_owe_money_are_dunned(conn, env):
    company_id = env["company_id"]
    acme = env["customer"]
    zero_only = seed_customer(conn, company_id, "Zero Balance Ltd")
    _level(conn, company_id, 1, 30, "call")
    _level(conn, company_id, 2, 60, "hold")

    # 48 and 33 days overdue on the run date: level 1.
    open_large = _submitted_invoice(conn, env, acme, "5", "250.00", "0", "2026-04-15")
    open_small = _submitted_invoice(conn, env, acme, "1", "9.50", "0", "2026-04-30")
    # 93 days overdue but fully discounted: nothing owed.
    acme_zero = _submitted_invoice(conn, env, acme, "2", "40.00", "100", "2026-03-01")
    # Not yet due on the run date.
    not_due = _submitted_invoice(conn, env, acme, "1", "300.00", "0", "2026-06-30")
    other_zero = _submitted_invoice(conn, env, zero_only, "1", "75.00", "100", "2026-03-01")

    assert _doc(conn, open_large) == ("submitted", "1250.00", "1250.00")
    assert _doc(conn, open_small) == ("submitted", "9.50", "9.50")
    assert _doc(conn, acme_zero) == ("submitted", "0.00", "0.00")
    assert _doc(conn, not_due) == ("submitted", "300.00", "300.00")
    assert _doc(conn, other_zero) == ("submitted", "0.00", "0.00")

    r = _run_cycle(conn, company_id)
    assert is_ok(r), r
    assert r["run_date"] == RUN_DATE
    assert r["customers_processed"] == 1
    assert r["runs_created"] == 1
    assert r["actions"] == dict(NO_ACTIONS, call=1)
    assert r["emails"] == {"sent": 0, "skipped": 0}

    runs = _runs(conn, company_id)
    assert len(runs) == 1
    run = runs[0]
    assert (run["customer_id"], run["level"], run["action_taken"], run["status"],
            run["run_date"], run["notes"]) == (
        acme, 1, "call", "completed", RUN_DATE, "2 overdue invoice(s)")
    dunned = set(json.loads(run["invoice_ids_json"]))
    assert dunned == {open_large, open_small}
    assert {i: _doc(conn, i)[2] for i in dunned} == {
        open_large: "1250.00", open_small: "9.50"}

    # The zero-outstanding invoices neither escalated Acme to the hold level
    # nor put the other customer on hold, and the cycle changed no invoice.
    assert _credit_status(conn, acme) == "active"
    assert _credit_status(conn, zero_only) == "active"
    assert _doc(conn, acme_zero) == ("submitted", "0.00", "0.00")
    assert _doc(conn, other_zero) == ("submitted", "0.00", "0.00")


def test_customer_whose_overdue_invoices_owe_nothing_is_not_put_on_hold(conn, env):
    company_id = env["company_id"]
    customer = env["customer"]
    _level(conn, company_id, 1, 30, "hold")
    zero = _submitted_invoice(conn, env, customer, "3", "12.00", "100", "2026-03-01")
    assert _doc(conn, zero) == ("submitted", "0.00", "0.00")

    r = _run_cycle(conn, company_id)
    assert is_ok(r), r
    assert r["customers_processed"] == 0
    assert r["runs_created"] == 0
    assert r["run_ids"] == []
    assert r["actions"] == NO_ACTIONS
    assert _runs(conn, company_id) == []
    assert _credit_status(conn, customer) == "active"


def test_missing_company_is_refused_and_writes_nothing(conn, env):
    company_id = env["company_id"]
    _level(conn, company_id, 1, 30, "hold")
    _submitted_invoice(conn, env, env["customer"], "1", "80.00", "0", "2026-03-01")

    r = call_action(mod.run_dunning_cycle, conn, ns(
        company_id=None, run_date=RUN_DATE, db_path=None))
    assert is_error(r)
    assert r["message"] == "--company-id is required"
    assert _runs(conn, company_id) == []
    assert _credit_status(conn, env["customer"]) == "active"


def test_partially_paid_and_overdue_invoices_are_dunned(conn, env):
    company_id = env["company_id"]
    acme = env["customer"]
    _level(conn, company_id, 1, 30, "call")
    inv_a = _submitted_invoice(conn, env, acme, "5", "250.00", "0", "2026-04-15")
    _receive_payment(conn, env, "500.00", "2026-05-01", [
        {"voucher_type": "sales_invoice", "voucher_id": inv_a,
         "allocated_amount": "500.00"},
    ])
    assert _doc(conn, inv_a) == ("partially_paid", "1250.00", "750.00")
    inv_b = _submitted_invoice(conn, env, acme, "1", "9.50", "0", "2026-04-30")
    # No action sets status 'overdue' today; set it directly.
    conn.execute(
        Q.update(_t_sales_invoice)
         .set(_t_sales_invoice.status, P())
         .where(_t_sales_invoice.id == P())
         .get_sql(),
        ("overdue", inv_b),
    )
    conn.commit()
    assert _doc(conn, inv_b) == ("overdue", "9.50", "9.50")

    r = _run_cycle(conn, company_id)
    assert is_ok(r), r
    assert r["customers_processed"] == 1
    assert r["runs_created"] == 1
    assert r["actions"] == dict(NO_ACTIONS, call=1)

    runs = _runs(conn, company_id)
    assert len(runs) == 1
    run = runs[0]
    assert run["notes"] == "2 overdue invoice(s)"
    assert set(json.loads(run["invoice_ids_json"])) == {inv_a, inv_b}
    assert _doc(conn, inv_a) == ("partially_paid", "1250.00", "750.00")
