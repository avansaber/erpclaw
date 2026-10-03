"""Bills carry the supplier currency and debit notes carry the bill currency."""
import importlib.util
import json
import os

from buying_helpers import call_action, is_error, is_ok, load_db_query, ns
from erpclaw_lib.query import P, Q, Table

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load_payments():
    spec = importlib.util.spec_from_file_location(
        "db_query_payments",
        os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "db_query.py"))
    pm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm)
    return pm


PAY = _load_payments()


def _eur_supplier(conn, env):
    sup_t = Table("supplier")
    uq = Q.update(sup_t).set(sup_t.default_currency, P()).where(sup_t.id == P())
    conn.execute(uq.get_sql(), ("EUR", env["supplier"]))
    conn.commit()


def _set_supplier_currency(conn, env, value):
    sup_t = Table("supplier")
    uq = Q.update(sup_t).set(sup_t.default_currency, P()).where(sup_t.id == P())
    conn.execute(uq.get_sql(), (value, env["supplier"]))
    conn.commit()


def _set_company_currency(conn, env, value):
    co_t = Table("company")
    uq = Q.update(co_t).set(co_t.default_currency, P()).where(co_t.id == P())
    conn.execute(uq.get_sql(), (value, env["company_id"]))
    conn.commit()


def _set_bill_exchange_rate(conn, bill_id, value):
    pi_t = Table("purchase_invoice")
    uq = Q.update(pi_t).set(pi_t.exchange_rate, P()).where(pi_t.id == P())
    conn.execute(uq.get_sql(), (value, bill_id))
    conn.commit()


def _read_bill(conn, bill_id):
    pi_t = Table("purchase_invoice")
    q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
    row = conn.execute(q.get_sql(), (bill_id,)).fetchone()
    return dict(row) if row else None


def _bill_ple(conn, bill_id):
    ple_t = Table("payment_ledger_entry")
    q = (Q.from_(ple_t).select(ple_t.star)
         .where(ple_t.voucher_type == P())
         .where(ple_t.voucher_id == P()))
    rows = conn.execute(q.get_sql(), ("purchase_invoice", bill_id)).fetchall()
    return [dict(r) for r in rows]


def _note_ple(conn, note_id):
    ple_t = Table("payment_ledger_entry")
    q = (Q.from_(ple_t).select(ple_t.star)
         .where(ple_t.voucher_type == P())
         .where(ple_t.voucher_id == P()))
    rows = conn.execute(q.get_sql(), ("debit_note", note_id)).fetchall()
    return [dict(r) for r in rows]


def _against_rows(conn, bill_id):
    ple_t = Table("payment_ledger_entry")
    q = (Q.from_(ple_t).select(ple_t.star)
         .where(ple_t.against_voucher_type == P())
         .where(ple_t.against_voucher_id == P())
         .where(ple_t.delinked == P()))
    rows = conn.execute(q.get_sql(), ("purchase_invoice", bill_id, 0)).fetchall()
    return [dict(r) for r in rows]


def _bill(conn, env):
    items = json.dumps([
        {"item_id": env["item1"], "qty": "10", "rate": "6.00",
         "warehouse_id": env["warehouse"]},
        {"item_id": env["item2"], "qty": "2", "rate": "20.00",
         "warehouse_id": env["warehouse"]},
    ])
    result = call_action(mod.create_purchase_invoice, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        purchase_order_id=None, purchase_receipt_id=None,
        tax_template_id=None, items=items))
    assert is_ok(result), result
    assert result["grand_total"] == "100.00"
    return result


def _submit(conn, bill_id):
    result = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=bill_id))
    assert is_ok(result), result
    return result


def _common_ns(**overrides):
    defaults = dict(
        supplier_id=None, company_id=None,
        items=None, frequency=None,
        start_date=None, end_date=None,
        tax_template_id=None, auto_submit=False,
        posting_date=None, name=None,
        blanket_order_id=None, blanket_status=None,
        sales_order_id=None,
        template_id=None, as_of_date=None,
        template_status=None,
        limit="20", offset="0",
    )
    defaults.update(overrides)
    return ns(**defaults)


def test_eur_supplier_bill_carries_eur(conn, env):
    _eur_supplier(conn, env)
    created = _bill(conn, env)
    assert created["currency"] == "EUR"
    bill_id = created["purchase_invoice_id"]
    stored = _read_bill(conn, bill_id)
    assert stored["currency"] == "EUR"
    assert stored["exchange_rate"] == "1"
    _submit(conn, bill_id)
    rows = _bill_ple(conn, bill_id)
    assert len(rows) == 1
    assert rows[0]["currency"] == "EUR"
    assert rows[0]["amount"] == "100.00"


def test_debit_note_copies_the_bill_currency(conn, env):
    _eur_supplier(conn, env)
    created = _bill(conn, env)
    bill_id = created["purchase_invoice_id"]
    _set_bill_exchange_rate(conn, bill_id, "1.0850")
    _submit(conn, bill_id)
    brows = _bill_ple(conn, bill_id)
    assert len(brows) == 1
    assert brows[0]["currency"] == "EUR"
    _set_supplier_currency(conn, env, "USD")
    items = json.dumps([{"item_id": env["item1"], "qty": "1"}])
    note = call_action(mod.create_debit_note, conn, ns(
        against_invoice_id=bill_id, posting_date="2026-06-25",
        reason="return", items=items))
    assert is_ok(note), note
    note_id = note["debit_note_id"]
    stored = _read_bill(conn, note_id)
    assert stored["currency"] == "EUR"
    assert stored["exchange_rate"] == "1.0850"
    _submit(conn, note_id)
    nrows = _note_ple(conn, note_id)
    assert len(nrows) == 1
    assert nrows[0]["currency"] == "EUR"
    assert nrows[0]["amount"] == "-6.00"


def test_usd_supplier_unchanged(conn, env):
    created = _bill(conn, env)
    assert created["currency"] == "USD"
    bill_id = created["purchase_invoice_id"]
    stored = _read_bill(conn, bill_id)
    assert stored["currency"] == "USD"
    assert stored["exchange_rate"] == "1"
    _submit(conn, bill_id)
    rows = _bill_ple(conn, bill_id)
    assert len(rows) == 1
    assert rows[0]["currency"] == "USD"


def test_empty_supplier_currency_falls_back_to_company(conn, env):
    _set_supplier_currency(conn, env, "")
    _set_company_currency(conn, env, "GBP")
    created = _bill(conn, env)
    stored = _read_bill(conn, created["purchase_invoice_id"])
    assert stored["currency"] == "GBP"


def test_recurring_bill_carries_the_supplier_currency(conn, env):
    _eur_supplier(conn, env)
    items = json.dumps([{"item_id": env["item1"], "qty": "1", "rate": "500.00"}])
    tmpl = call_action(mod.add_recurring_bill_template, conn, _common_ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        items=items, frequency="monthly",
        start_date="2026-01-01", end_date="2026-12-31"))
    assert is_ok(tmpl), tmpl
    activated = call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id=tmpl["template_id"], template_status="active"))
    assert is_ok(activated), activated
    result = call_action(mod.generate_recurring_bills, conn, _common_ns(
        company_id=env["company_id"], as_of_date="2026-01-15"))
    assert is_ok(result), result
    assert result["bills_generated"] >= 1
    bill_id = result["bills"][0]["invoice_id"]
    stored = _read_bill(conn, bill_id)
    assert stored["currency"] == "EUR"


def _pay_args(env, bill_id, currency):
    allocations = json.dumps([{
        "voucher_type": "purchase_invoice", "voucher_id": bill_id,
        "allocated_amount": "100.00"}])
    return ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date="2026-06-25", party_type="supplier",
        party_id=env["supplier"], paid_from_account=env["cash"],
        paid_to_account=env["ap"], paid_amount="100.00",
        exchange_rate=None, payment_currency=currency,
        reference_number=None, reference_date=None,
        allocations=allocations, deductions=None)


def test_usd_payment_against_eur_bill_is_refused(conn, env):
    _eur_supplier(conn, env)
    bill_id = _bill(conn, env)["purchase_invoice_id"]
    _submit(conn, bill_id)
    created = call_action(PAY.add_payment, conn, _pay_args(env, bill_id, None))
    assert is_ok(created), created
    refused = call_action(
        PAY.submit_payment, conn, ns(payment_entry_id=created["payment_entry_id"]))
    assert is_error(refused)
    assert refused.get("message") == (
        "currency mismatch: invoice in EUR, payment in USD; "
        "invoice currency must equal payment currency")
    stored = _read_bill(conn, bill_id)
    assert stored["outstanding_amount"] == "100.00"


def test_eur_payment_against_eur_bill_submits(conn, env):
    _eur_supplier(conn, env)
    bill_id = _bill(conn, env)["purchase_invoice_id"]
    _submit(conn, bill_id)
    created = call_action(PAY.add_payment, conn, _pay_args(env, bill_id, "EUR"))
    assert is_ok(created), created
    done = call_action(
        PAY.submit_payment, conn, ns(payment_entry_id=created["payment_entry_id"]))
    assert is_ok(done), done
    stored = _read_bill(conn, bill_id)
    assert stored["outstanding_amount"] == "0"
    assert stored["status"] == "paid"
    rows = _against_rows(conn, bill_id)
    assert len(rows) >= 1
    for row in rows:
        assert row["currency"] == "EUR"
