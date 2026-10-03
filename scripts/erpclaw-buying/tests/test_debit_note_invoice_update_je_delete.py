"""Behaviour of create-debit-note, update-purchase-invoice, update-sales-invoice
and delete-journal-entry, read back from the database.

create-debit-note and update-purchase-invoice live in erpclaw-buying (this
directory). update-sales-invoice and delete-journal-entry live in the sibling
erpclaw-selling and erpclaw-journals domains; their db_query.py files are
loaded by path, and every domain runs against the same full-schema database the
`conn` fixture builds.

Scenario (all dates fixed, service items only so no stock ledger is involved):
  - Original bill: Consulting 10 x 100.00 + Support Plan 4 x 62.50 = 1250.00,
    submitted on 2026-06-20.
  - Debit note on 2026-06-25: Consulting 3 (rate looked up from the bill,
    100.00) + Support Plan 2 x 62.50 = -425.00.
  - Purchase tax 7.5 %, sales tax 8.25 %, both on net total.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from buying_helpers import call_action, is_error, is_ok, load_db_query, ns  # noqa: E402

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load_domain(domain, module_name):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(_SCRIPTS_DIR, domain, "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = load_db_query()
S = _load_domain("erpclaw-selling", "db_query_selling_for_buying_tests")
J = _load_domain("erpclaw-journals", "db_query_journals_for_buying_tests")

BILL_DATE = "2026-06-20"
NOTE_DATE = "2026-06-25"


def _u():
    return str(uuid.uuid4())


def _msg(result):
    return result.get("message", "")


def _service_item(conn, name):
    iid = _u()
    conn.execute(
        "INSERT INTO item (id, item_name, item_code, stock_uom, is_stock_item) "
        "VALUES (?, ?, ?, 'Hour', 0)", (iid, name, f"SVC-{iid[:6]}"))
    conn.commit()
    return iid


def _account(conn, company_id, name, root_type, account_type, number):
    aid = _u()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type, "
        "balance_direction, company_id, depth) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (aid, name, number, root_type, account_type, direction, company_id))
    conn.commit()
    return aid


def _tax_template(conn, company_id, name, tax_type, account_id, rate):
    tid = _u()
    conn.execute(
        "INSERT INTO tax_template (id, name, tax_type, company_id) VALUES (?, ?, ?, ?)",
        (tid, name, tax_type, company_id))
    conn.execute(
        "INSERT INTO tax_template_line (id, tax_template_id, tax_account_id, rate, "
        "charge_type, row_order, add_deduct) VALUES (?, ?, ?, ?, 'on_net_total', 0, 'add')",
        (_u(), tid, account_id, rate))
    conn.commit()
    return tid


def _setup(conn, env):
    cid = env["company_id"]
    env["svc1"] = _service_item(conn, "Consulting")
    env["svc2"] = _service_item(conn, "Support Plan")
    env["input_tax"] = _account(conn, cid, "Input Tax", "asset", "tax", "1400")
    env["ar"] = _account(conn, cid, "Accounts Receivable", "asset", "receivable", "1100")
    env["revenue"] = _account(conn, cid, "Service Revenue", "income", "revenue", "4000")
    env["sales_tax"] = _account(conn, cid, "Sales Tax Payable", "liability", "tax", "2200")
    conn.execute(
        "UPDATE company SET default_receivable_account_id = ?, "
        "default_income_account_id = ? WHERE id = ?", (env["ar"], env["revenue"], cid))
    env["customer"] = _u()
    conn.execute(
        "INSERT INTO customer (id, name, company_id, customer_type, status, credit_limit) "
        "VALUES (?, 'Acme Corp', ?, 'company', 'active', '0')", (env["customer"], cid))
    conn.commit()
    env["purchase_tax"] = _tax_template(conn, cid, "Purchase Tax 7.5", "purchase",
                                        env["input_tax"], "7.5")
    env["sales_tax_tpl"] = _tax_template(conn, cid, "Sales Tax 8.25", "sales",
                                         env["sales_tax"], "8.25")
    return env


def _bill(conn, env, lines, tax_template_id=None, due_date="2026-07-20"):
    r = call_action(B.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date=BILL_DATE, due_date=due_date,
        items=json.dumps([{"item_id": env[k], "qty": q, "rate": rt} for k, q, rt in lines]),
        tax_template_id=tax_template_id))
    assert is_ok(r), r
    return r["purchase_invoice_id"]


def _submit_bill(conn, pi_id):
    r = call_action(B.submit_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
    assert is_ok(r), r
    return r


def _submitted_original(conn, env):
    pi_id = _bill(conn, env, [("svc1", "10", "100.00"), ("svc2", "4", "62.50")])
    _submit_bill(conn, pi_id)
    return pi_id


def _debit_note(conn, env, pi_id):
    return call_action(B.create_debit_note, conn, ns(
        against_invoice_id=pi_id, posting_date=NOTE_DATE, reason="Hours not delivered",
        items=json.dumps([{"item_id": env["svc1"], "qty": "3"},
                          {"item_id": env["svc2"], "qty": "2", "rate": "62.50"}])))


def _row(conn, table, row_id):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


def _rows(conn, sql, params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _gl(conn, voucher_type, voucher_id):
    return conn.execute(
        "SELECT account_id, debit, credit, party_type, party_id, posting_date, "
        "voucher_type, voucher_id, is_cancelled FROM gl_entry "
        "WHERE voucher_type = ? AND voucher_id = ?", (voucher_type, voucher_id)).fetchall()


def _legs(rows):
    return sorted((r["account_id"], r["debit"], r["credit"]) for r in rows)


def _balanced(rows):
    debit = sum((Decimal(r["debit"]) for r in rows), Decimal("0"))
    credit = sum((Decimal(r["credit"]) for r in rows), Decimal("0"))
    return str(debit), str(credit)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _pi_items(conn, pi_id):
    return sorted((r["item_id"], r["quantity"], r["rate"], r["amount"])
                  for r in conn.execute(
                      "SELECT item_id, quantity, rate, amount FROM purchase_invoice_item "
                      "WHERE purchase_invoice_id = ?", (pi_id,)).fetchall())


# ---------------------------------------------------------------------------
# create-debit-note
# ---------------------------------------------------------------------------

def test_create_debit_note_writes_negative_draft_against_the_bill(conn, env):
    env = _setup(conn, env)
    pi_id = _submitted_original(conn, env)
    gl_before = _count(conn, "gl_entry")
    ple_before = _count(conn, "payment_ledger_entry")

    r = _debit_note(conn, env, pi_id)
    assert is_ok(r), r
    dn_id = r["debit_note_id"]
    assert r["total_amount"] == "-425.00"

    dn = _row(conn, "purchase_invoice", dn_id)
    assert dn["status"] == "draft"
    assert dn["is_return"] == 1
    assert dn["return_against"] == pi_id
    assert dn["supplier_id"] == env["supplier"]
    assert dn["company_id"] == env["company_id"]
    assert dn["posting_date"] == NOTE_DATE
    assert (dn["total_amount"], dn["tax_amount"], dn["grand_total"],
            dn["outstanding_amount"]) == ("-425.00", "0", "-425.00", "-425.00")
    assert _pi_items(conn, dn_id) == sorted([
        (env["svc1"], "-3.00", "100.00", "-300.00"),
        (env["svc2"], "-2.00", "62.50", "-125.00"),
    ])

    # A draft posts nothing, and the original bill is untouched.
    assert _count(conn, "gl_entry") == gl_before
    assert _count(conn, "payment_ledger_entry") == ple_before
    orig = _row(conn, "purchase_invoice", pi_id)
    assert (orig["status"], orig["grand_total"], orig["outstanding_amount"]) == \
        ("submitted", "1250.00", "1250.00")


def test_submitted_debit_note_posts_mirror_gl_and_negative_payable(conn, env):
    env = _setup(conn, env)
    pi_id = _submitted_original(conn, env)
    dn_id = _debit_note(conn, env, pi_id)["debit_note_id"]

    r = call_action(B.submit_purchase_invoice, conn, ns(purchase_invoice_id=dn_id))
    assert is_ok(r), r
    assert r["voucher_type"] == "debit_note"

    orig_gl = _gl(conn, "purchase_invoice", pi_id)
    assert _legs(orig_gl) == sorted([
        (env["expense"], "1000.00", "0.00"),
        (env["expense"], "250.00", "0.00"),
        (env["ap"], "0.00", "1250.00"),
    ])

    dn_gl = _gl(conn, "debit_note", dn_id)
    assert _legs(dn_gl) == sorted([
        (env["expense"], "0.00", "300.00"),
        (env["expense"], "0.00", "125.00"),
        (env["ap"], "425.00", "0.00"),
    ])
    assert _balanced(dn_gl) == ("425.00", "425.00")
    assert {(g["posting_date"], g["voucher_type"], g["voucher_id"], g["is_cancelled"])
            for g in dn_gl} == {(NOTE_DATE, "debit_note", dn_id, 0)}
    ap_leg = [g for g in dn_gl if g["account_id"] == env["ap"]][0]
    assert (ap_leg["party_type"], ap_leg["party_id"]) == ("supplier", env["supplier"])

    # Payable for the supplier nets to 1250.00 - 425.00.
    ap_rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE account_id = ? AND party_id = ? "
        "AND is_cancelled = 0", (env["ap"], env["supplier"])).fetchall()
    ap_net = sum((Decimal(g["credit"]) - Decimal(g["debit"]) for g in ap_rows), Decimal("0"))
    assert str(ap_net) == "825.00"

    ple = _rows(conn,
                "SELECT voucher_type, voucher_id, against_voucher_type, against_voucher_id, "
                "account_id, party_type, party_id, amount FROM payment_ledger_entry "
                "WHERE party_id = ?", (env["supplier"],))
    assert sorted(ple, key=lambda p: p["amount"]) == [
        {"voucher_type": "debit_note", "voucher_id": dn_id,
         "against_voucher_type": "purchase_invoice", "against_voucher_id": pi_id,
         "account_id": env["ap"], "party_type": "supplier", "party_id": env["supplier"],
         "amount": "-425.00"},
        {"voucher_type": "purchase_invoice", "voucher_id": pi_id,
         "against_voucher_type": "purchase_invoice", "against_voucher_id": pi_id,
         "account_id": env["ap"], "party_type": "supplier", "party_id": env["supplier"],
         "amount": "1250.00"},
    ]

    # The note carries its own negative outstanding; the bill keeps its own.
    dn = _row(conn, "purchase_invoice", dn_id)
    orig = _row(conn, "purchase_invoice", pi_id)
    assert (dn["status"], dn["outstanding_amount"]) == ("submitted", "-425.00")
    assert (orig["status"], orig["outstanding_amount"]) == ("submitted", "1250.00")
    assert _count(conn, "stock_ledger_entry") == 0


def test_create_debit_note_refuses_draft_and_unknown_bill(conn, env):
    env = _setup(conn, env)
    draft_id = _bill(conn, env, [("svc1", "10", "100.00")])
    pi_before = _count(conn, "purchase_invoice")
    items_before = _count(conn, "purchase_invoice_item")

    r = _debit_note(conn, env, draft_id)
    assert is_error(r)
    assert _msg(r) == "Cannot create debit note: invoice status is 'draft'"

    missing = _u()
    r = _debit_note(conn, env, missing)
    assert is_error(r)
    assert _msg(r) == f"Purchase invoice {missing} not found"

    assert _count(conn, "purchase_invoice") == pi_before
    assert _count(conn, "purchase_invoice_item") == items_before
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM purchase_invoice WHERE is_return = 1").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# update-purchase-invoice
# ---------------------------------------------------------------------------

def test_update_purchase_invoice_recomputes_totals_tax_and_feeds_submit(conn, env):
    env = _setup(conn, env)
    pi_id = _bill(conn, env, [("svc1", "3", "45.50"), ("svc2", "2", "19.99")],
                  tax_template_id=env["purchase_tax"])
    before = _row(conn, "purchase_invoice", pi_id)
    assert (before["total_amount"], before["tax_amount"], before["grand_total"],
            before["outstanding_amount"]) == ("176.48", "13.24", "189.72", "189.72")
    old_item_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM purchase_invoice_item WHERE purchase_invoice_id = ?", (pi_id,))}

    r = call_action(B.update_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id, due_date="2026-08-15",
        items=json.dumps([{"item_id": env["svc1"], "qty": "5", "rate": "45.50"},
                          {"item_id": env["svc2"], "qty": "2", "rate": "21.33"}])))
    assert is_ok(r), r
    assert r["updated_fields"] == ["due_date", "items"]

    pi = _row(conn, "purchase_invoice", pi_id)
    assert pi["status"] == "draft"
    assert pi["due_date"] == "2026-08-15"
    # 227.50 + 42.66 = 270.16; 7.5 % = 20.262 -> 20.26
    assert (pi["total_amount"], pi["tax_amount"], pi["grand_total"],
            pi["outstanding_amount"]) == ("270.16", "20.26", "290.42", "290.42")
    assert _pi_items(conn, pi_id) == sorted([
        (env["svc1"], "5.00", "45.50", "227.50"),
        (env["svc2"], "2.00", "21.33", "42.66"),
    ])
    new_item_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM purchase_invoice_item WHERE purchase_invoice_id = ?", (pi_id,))}
    assert len(new_item_ids) == 2 and not (new_item_ids & old_item_ids)

    _submit_bill(conn, pi_id)
    gl = _gl(conn, "purchase_invoice", pi_id)
    assert _legs(gl) == sorted([
        (env["expense"], "227.50", "0.00"),
        (env["expense"], "42.66", "0.00"),
        (env["input_tax"], "20.26", "0.00"),
        (env["ap"], "0.00", "290.42"),
    ])
    assert _balanced(gl) == ("290.42", "290.42")
    assert conn.execute(
        "SELECT amount FROM payment_ledger_entry WHERE voucher_id = ?",
        (pi_id,)).fetchone()["amount"] == "290.42"


def test_update_purchase_invoice_refuses_submitted_and_empty_update(conn, env):
    env = _setup(conn, env)
    pi_id = _bill(conn, env, [("svc1", "2", "100.00")])
    _submit_bill(conn, pi_id)
    header = _row(conn, "purchase_invoice", pi_id)
    items = _rows(conn, "SELECT * FROM purchase_invoice_item WHERE purchase_invoice_id = ?",
                  (pi_id,))
    gl = _legs(_gl(conn, "purchase_invoice", pi_id))

    r = call_action(B.update_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id, due_date="2026-09-30",
        items=json.dumps([{"item_id": env["svc1"], "qty": "9", "rate": "100.00"}])))
    assert is_error(r)
    assert _msg(r) == "Cannot update: invoice is 'submitted' (must be 'draft')"
    assert _row(conn, "purchase_invoice", pi_id) == header
    assert header["grand_total"] == "200.00"
    assert _rows(conn, "SELECT * FROM purchase_invoice_item WHERE purchase_invoice_id = ?",
                 (pi_id,)) == items
    assert _legs(_gl(conn, "purchase_invoice", pi_id)) == gl

    draft_id = _bill(conn, env, [("svc2", "1", "62.50")])
    draft = _row(conn, "purchase_invoice", draft_id)
    r = call_action(B.update_purchase_invoice, conn, ns(
        purchase_invoice_id=draft_id, due_date=None, items=None))
    assert is_error(r)
    assert _msg(r) == "No fields to update"
    assert _row(conn, "purchase_invoice", draft_id) == draft


# ---------------------------------------------------------------------------
# update-sales-invoice
# ---------------------------------------------------------------------------

def _sales_invoice(conn, env, lines):
    r = call_action(S.create_sales_invoice, conn, ns(
        company_id=env["company_id"], customer_id=env["customer"],
        tax_template_id=env["sales_tax_tpl"], sales_order_id=None,
        delivery_note_id=None, posting_date=BILL_DATE, due_date="2026-07-20",
        payment_terms_id=None,
        items=json.dumps([{"item_id": env[k], "qty": q, "rate": rt} for k, q, rt in lines])))
    assert is_ok(r), r
    return r["sales_invoice_id"]


def _si_items(conn, si_id):
    return sorted((r["item_id"], r["quantity"], r["rate"], r["amount"],
                   r["discount_percentage"], r["net_amount"])
                  for r in conn.execute(
                      "SELECT item_id, quantity, rate, amount, discount_percentage, "
                      "net_amount FROM sales_invoice_item WHERE sales_invoice_id = ?",
                      (si_id,)).fetchall())


def test_update_sales_invoice_recomputes_totals_tax_and_feeds_submit(conn, env):
    env = _setup(conn, env)
    si_id = _sales_invoice(conn, env, [("svc1", "4", "125.00"), ("svc2", "1", "80.00")])
    before = _row(conn, "sales_invoice", si_id)
    assert (before["total_amount"], before["tax_amount"], before["grand_total"]) == \
        ("580.00", "47.85", "627.85")

    r = call_action(S.update_sales_invoice, conn, ns(
        sales_invoice_id=si_id, due_date=None,
        items=json.dumps([
            {"item_id": env["svc1"], "qty": "6", "rate": "125.00"},
            {"item_id": env["svc2"], "qty": "3", "rate": "80.00",
             "discount_percentage": "10"}])))
    assert is_ok(r), r
    assert r["updated_fields"] == ["items", "total_amount", "tax_amount", "grand_total"]

    si = _row(conn, "sales_invoice", si_id)
    assert si["status"] == "draft"
    assert si["due_date"] == "2026-07-20"
    # 750.00 + 216.00 = 966.00; 8.25 % = 79.695 -> 79.70 (half up)
    assert (si["total_amount"], si["tax_amount"], si["grand_total"],
            si["outstanding_amount"]) == ("966.00", "79.70", "1045.70", "1045.70")
    assert _si_items(conn, si_id) == sorted([
        (env["svc1"], "6.00", "125.00", "750.00", "0.00", "750.00"),
        (env["svc2"], "3.00", "80.00", "240.00", "10.00", "216.00"),
    ])

    r = call_action(S.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r), r
    gl = _gl(conn, "sales_invoice", si_id)
    assert _legs(gl) == sorted([
        (env["ar"], "1045.70", "0.00"),
        (env["revenue"], "0.00", "966.00"),
        (env["sales_tax"], "0.00", "79.70"),
    ])
    assert _balanced(gl) == ("1045.70", "1045.70")
    assert conn.execute(
        "SELECT amount FROM payment_ledger_entry WHERE voucher_id = ?",
        (si_id,)).fetchone()["amount"] == "1045.70"


def test_update_sales_invoice_refuses_submitted_and_empty_update(conn, env):
    env = _setup(conn, env)
    si_id = _sales_invoice(conn, env, [("svc1", "4", "125.00")])
    r = call_action(S.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r), r
    header = _row(conn, "sales_invoice", si_id)
    items = _rows(conn, "SELECT * FROM sales_invoice_item WHERE sales_invoice_id = ?",
                  (si_id,))
    gl = _legs(_gl(conn, "sales_invoice", si_id))

    r = call_action(S.update_sales_invoice, conn, ns(
        sales_invoice_id=si_id, due_date="2026-09-30",
        items=json.dumps([{"item_id": env["svc1"], "qty": "1", "rate": "1.00"}])))
    assert is_error(r)
    assert _msg(r) == "Cannot update: sales invoice is 'submitted' (must be 'draft')"
    assert _row(conn, "sales_invoice", si_id) == header
    assert header["grand_total"] == "541.25"
    assert _rows(conn, "SELECT * FROM sales_invoice_item WHERE sales_invoice_id = ?",
                 (si_id,)) == items
    assert _legs(_gl(conn, "sales_invoice", si_id)) == gl

    draft_id = _sales_invoice(conn, env, [("svc2", "1", "80.00")])
    draft = _row(conn, "sales_invoice", draft_id)
    r = call_action(S.update_sales_invoice, conn, ns(
        sales_invoice_id=draft_id, due_date=None, items=None))
    assert is_error(r)
    assert _msg(r) == "No fields to update"
    assert _row(conn, "sales_invoice", draft_id) == draft


# ---------------------------------------------------------------------------
# delete-journal-entry
# ---------------------------------------------------------------------------

def _journal(conn, env, amount):
    r = call_action(J.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date=BILL_DATE, entry_type="journal",
        remark="Accrued services", cwip_asset_id=None,
        lines=json.dumps([
            {"account_id": env["expense"], "debit": amount, "credit": "0",
             "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": amount}])))
    assert is_ok(r), r
    return r["journal_entry_id"]


def _je_lines(conn, je_id):
    return sorted((r["account_id"], r["debit"], r["credit"]) for r in conn.execute(
        "SELECT account_id, debit, credit FROM journal_entry_line "
        "WHERE journal_entry_id = ?", (je_id,)).fetchall())


def test_delete_draft_journal_entry_removes_header_and_lines_only(conn, env):
    je_id = _journal(conn, env, "150.00")
    keep_id = _journal(conn, env, "75.25")
    assert _je_lines(conn, je_id) == sorted([
        (env["expense"], "150.00", "0.00"), (env["cash"], "0.00", "150.00")])

    r = call_action(J.delete_journal_entry, conn, ns(journal_entry_id=je_id))
    assert is_ok(r), r
    assert r["deleted"] is True

    assert conn.execute("SELECT COUNT(*) AS n FROM journal_entry WHERE id = ?",
                        (je_id,)).fetchone()["n"] == 0
    assert _je_lines(conn, je_id) == []
    assert _count(conn, "gl_entry") == 0
    audit_rows = _rows(conn, "SELECT skill, action, entity_type FROM audit_log "
                             "WHERE entity_id = ? AND action = ?",
                       (je_id, "delete-journal-entry"))
    assert audit_rows == [{"skill": "erpclaw-journals", "action": "delete-journal-entry",
                           "entity_type": "journal_entry"}]

    # The other draft is untouched.
    kept = _row(conn, "journal_entry", keep_id)
    assert (kept["status"], kept["total_debit"], kept["total_credit"]) == \
        ("draft", "75.25", "75.25")
    assert _je_lines(conn, keep_id) == sorted([
        (env["expense"], "75.25", "0.00"), (env["cash"], "0.00", "75.25")])


def test_delete_journal_entry_refuses_submitted_cancelled_and_unknown(conn, env):
    je_id = _journal(conn, env, "150.00")
    r = call_action(J.submit_journal_entry, conn, ns(journal_entry_id=je_id))
    assert is_ok(r), r
    header = _row(conn, "journal_entry", je_id)
    lines = _je_lines(conn, je_id)
    gl = _gl(conn, "journal_entry", je_id)
    assert _legs(gl) == sorted([
        (env["expense"], "150.00", "0.00"), (env["cash"], "0.00", "150.00")])

    r = call_action(J.delete_journal_entry, conn, ns(journal_entry_id=je_id))
    assert is_error(r)
    assert _msg(r) == ("Cannot delete: journal entry is 'submitted' "
                       "(only 'draft' can be deleted)")
    assert _row(conn, "journal_entry", je_id) == header
    assert _je_lines(conn, je_id) == lines
    assert [dict(g) for g in _gl(conn, "journal_entry", je_id)] == [dict(g) for g in gl]

    r = call_action(J.cancel_journal_entry, conn, ns(journal_entry_id=je_id))
    assert is_ok(r), r
    gl_after_cancel = _count(conn, "gl_entry")
    r = call_action(J.delete_journal_entry, conn, ns(journal_entry_id=je_id))
    assert is_error(r)
    assert _msg(r) == ("Cannot delete: journal entry is 'cancelled' "
                       "(only 'draft' can be deleted)")
    assert _row(conn, "journal_entry", je_id)["status"] == "cancelled"
    assert _je_lines(conn, je_id) == lines
    assert _count(conn, "gl_entry") == gl_after_cancel

    missing = _u()
    r = call_action(J.delete_journal_entry, conn, ns(journal_entry_id=missing))
    assert is_error(r)
    assert _msg(r) == f"Journal entry {missing} not found"
    r = call_action(J.delete_journal_entry, conn, ns(journal_entry_id=None))
    assert is_error(r)
    assert _msg(r) == "--journal-entry-id is required"
    assert _count(conn, "journal_entry") == 1
