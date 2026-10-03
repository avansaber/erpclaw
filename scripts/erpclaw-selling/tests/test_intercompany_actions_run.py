"""m677 — the five intercompany actions run on every install.

Fresh installs declare ``sales_invoice.is_intercompany`` /
``.intercompany_reference_id``, the ``purchase_invoice`` twins and the
``intercompany_account_map`` table (migration 044 carries existing installs
there), so ``add-intercompany-account-map`` stores, ``create`` mirrors a
draft bill, both ``list`` actions read, and ``cancel-intercompany-invoice``
reverses the ledger and the stock ledger — or refuses, writing nothing,
when the stock reversal fails or the invoice is already cancelled.

Buyer company B is built exactly as ``_mirror_env`` in
``test_cancel_intercompany_invoice_behaviour.py`` builds it. Every sales
invoice below is ``create_sales_invoice`` + ``submit_sales_invoice`` with
item1 qty "10" rate "100.00" at the warehouse, posting "2026-06-20", due
"2026-07-20": grand total "1000.00", four GL rows, one SLE row.

Reads go through PyPika (``from erpclaw_lib.query import Q, P, Table``).
Money is text: exact string comparisons, never float.
"""
import importlib.util
import json
import os
from unittest import mock

from selling_helpers import (
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_account,
    seed_company,
    seed_cost_center,
    seed_fiscal_year,
    seed_naming_series,
    seed_supplier,
)

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))  # scripts/


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The mirror leg is submitted through the real buying action (the same idiom
# the behaviour suite uses), so no test-side re-implementation creeps in.
buy = _load("db_query_buying_m677", "erpclaw-buying/db_query.py")

from erpclaw_lib.query import Q, P, Table  # noqa: E402

POSTING_DATE = "2026-06-20"
DUE_DATE = "2026-07-20"

_SNAP_TABLES = ("sales_invoice", "sales_invoice_item", "purchase_invoice",
                "purchase_invoice_item", "intercompany_account_map",
                "gl_entry", "stock_ledger_entry", "payment_ledger_entry",
                "audit_log")


def _snapshot(conn):
    """Every row of the nine intercompany-touched tables, sorted by id."""
    snap = {}
    for name in _SNAP_TABLES:
        t = Table(name)
        rows = conn.execute(
            Q.from_(t).select(t.star).orderby(t.id).get_sql()).fetchall()
        snap[name] = [dict(r) for r in rows]
    return snap


def _mirror_env(conn):
    """Company B (the second company): payable + expense + supplier + series."""
    company_id = seed_company(conn, "Buyer Co", "BC")
    seed_fiscal_year(conn, company_id)
    seed_cost_center(conn, company_id, "Main CC")
    payable = seed_account(conn, company_id, "Accounts Payable",
                           "liability", "payable", "2000")
    expense = seed_account(conn, company_id, "Purchases",
                           "expense", "expense", "5000")
    conn.execute(
        "UPDATE company SET default_payable_account_id = ?, "
        "default_expense_account_id = ? WHERE id = ?",
        (payable, expense, company_id),
    )
    conn.commit()
    supplier = seed_supplier(conn, company_id, "Seller As Supplier")
    seed_naming_series(conn, company_id)
    return {"company_id": company_id, "payable": payable,
            "expense": expense, "supplier": supplier}


def _make_invoice(conn, env):
    """Real create + submit of the stock sales invoice every test uses."""
    items = json.dumps([{
        "item_id": env["item1"], "qty": "10", "rate": "100.00",
        "warehouse_id": env["warehouse"],
    }])
    created = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date=POSTING_DATE, due_date=DUE_DATE,
        items=items, tax_template_id=None, payment_terms_id=None,
    ))
    assert is_ok(created), created
    si_id = created["sales_invoice_id"]
    submitted = call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(submitted), submitted
    return si_id


def _add_map(conn, env, menv):
    result = call_action(mod.add_intercompany_account_map, conn, ns(
        company_id=env["company_id"],
        target_company_id=menv["company_id"],
        source_account_id=env["revenue"],
        target_account_id=menv["expense"],
    ))
    assert is_ok(result), result
    return result["map_id"]


def _make_mirror(conn, si_id, menv):
    result = call_action(mod.create_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id, target_company_id=menv["company_id"],
        supplier_id=menv["supplier"]))
    assert is_ok(result), result
    return result


def _prepare(conn, env):
    """Invoice + map + draft mirror, the shared setup for tests 3-8."""
    menv = _mirror_env(conn)
    si_id = _make_invoice(conn, env)
    _add_map(conn, env, menv)
    created = _make_mirror(conn, si_id, menv)
    return menv, si_id, created["purchase_invoice_id"]


def _row(conn, table, doc_id):
    t = Table(table)
    row = conn.execute(
        Q.from_(t).select(t.star).where(t.id == P()).get_sql(),
        (doc_id,)).fetchone()
    return dict(row) if row else None


def _invoice_gl(conn, si_id):
    """(account name, debit, credit, is_cancelled, posting_date), sorted."""
    g = Table("gl_entry")
    a = Table("account")
    q = (Q.from_(g).join(a).on(a.id == g.account_id)
         .select(a.name, g.debit, g.credit, g.is_cancelled, g.posting_date)
         .where(g.voucher_id == P()))
    rows = conn.execute(q.get_sql(), (si_id,)).fetchall()
    return sorted([(r["name"], r["debit"], r["credit"], r["is_cancelled"],
                    r["posting_date"]) for r in rows])


def _invoice_sle(conn, si_id):
    s = Table("stock_ledger_entry")
    q = (Q.from_(s).select(s.actual_qty, s.is_cancelled)
         .where(s.voucher_id == P()))
    rows = conn.execute(q.get_sql(), (si_id,)).fetchall()
    return sorted([(r["actual_qty"], r["is_cancelled"]) for r in rows])


# ── 1. the map stores and refuses a duplicate ───────────────────────────────

def test_add_map_stores_row_and_refuses_duplicate(conn, env):
    menv = _mirror_env(conn)
    gl_before = _snapshot(conn)["gl_entry"]
    added = call_action(mod.add_intercompany_account_map, conn, ns(
        company_id=env["company_id"],
        target_company_id=menv["company_id"],
        source_account_id=env["revenue"],
        target_account_id=menv["expense"],
    ))
    assert is_ok(added), added
    stored = _row(conn, "intercompany_account_map", added["map_id"])
    assert stored is not None
    assert stored["source_company_id"] == env["company_id"]
    assert stored["target_company_id"] == menv["company_id"]
    assert stored["source_account_id"] == env["revenue"]
    assert stored["target_account_id"] == menv["expense"]
    assert _snapshot(conn)["gl_entry"] == gl_before

    before = _snapshot(conn)
    refused = call_action(mod.add_intercompany_account_map, conn, ns(
        company_id=env["company_id"],
        target_company_id=menv["company_id"],
        source_account_id=env["revenue"],
        target_account_id=menv["expense"],
    ))
    assert is_error(refused)
    assert refused["message"] == \
        "Mapping already exists for this source account and company pair"
    assert _snapshot(conn) == before


# ── 2. the listing names both accounts ──────────────────────────────────────

def test_list_maps_returns_names(conn, env):
    menv = _mirror_env(conn)
    _add_map(conn, env, menv)
    result = call_action(mod.list_intercompany_account_maps, conn, ns(
        company_id=env["company_id"],
        target_company_id=menv["company_id"]))
    assert is_ok(result), result
    assert result["total"] == 1
    assert result["mappings"][0]["source_account_name"] == "Sales Revenue"
    assert result["mappings"][0]["target_account_name"] == "Purchases"


# ── 3. create mirrors a draft bill ──────────────────────────────────────────

def test_create_mirrors_a_draft_bill(conn, env):
    menv = _mirror_env(conn)
    si_id = _make_invoice(conn, env)
    _add_map(conn, env, menv)
    ledgers_before = {name: _snapshot(conn)[name]
                      for name in ("gl_entry", "stock_ledger_entry",
                                   "payment_ledger_entry")}
    created = _make_mirror(conn, si_id, menv)
    assert created["total_amount"] == "1000.00"
    assert created["grand_total"] == "1000.00"
    assert created["items_mirrored"] == 1
    pi_id = created["purchase_invoice_id"]

    bill = _row(conn, "purchase_invoice", pi_id)
    assert bill["status"] == "draft"
    assert bill["update_stock"] == 0
    assert bill["is_intercompany"] == 1
    assert bill["intercompany_reference_id"] == si_id
    assert bill["company_id"] == menv["company_id"]
    assert bill["supplier_id"] == menv["supplier"]
    assert bill["total_amount"] == "1000.00"
    assert bill["grand_total"] == "1000.00"
    assert bill["outstanding_amount"] == "1000.00"
    assert bill["tax_amount"] == "0"

    pii = Table("purchase_invoice_item")
    items = conn.execute(
        Q.from_(pii).select(pii.star)
        .where(pii.purchase_invoice_id == P()).get_sql(),
        (pi_id,)).fetchall()
    assert len(items) == 1
    assert items[0]["item_id"] == env["item1"]
    assert items[0]["quantity"] == "10.00"
    assert items[0]["rate"] == "100.00"
    assert items[0]["amount"] == "1000.00"
    assert items[0]["expense_account_id"] == menv["expense"]

    invoice = _row(conn, "sales_invoice", si_id)
    assert invoice["is_intercompany"] == 1
    assert invoice["intercompany_reference_id"] == pi_id

    after = _snapshot(conn)
    for name, rows in ledgers_before.items():
        assert after[name] == rows

    before = _snapshot(conn)
    refused = call_action(mod.create_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id, target_company_id=menv["company_id"],
        supplier_id=menv["supplier"]))
    assert is_error(refused)
    assert refused["message"] == \
        "Sales invoice is already an intercompany invoice"
    assert _snapshot(conn) == before


# ── 4. the listing shows both directions ────────────────────────────────────

def test_list_invoices_both_directions(conn, env):
    menv, si_id, pi_id = _prepare(conn, env)
    seller = call_action(mod.list_intercompany_invoices, conn, ns(
        company_id=env["company_id"], limit="20", offset="0"))
    assert is_ok(seller), seller
    assert seller["total"] == 1
    row = seller["invoices"][0]
    assert row["direction"] == "sales"
    assert row["grand_total"] == "1000.00"
    assert row["customer_name"] == "Acme Corp"
    assert row["intercompany_reference_id"] == pi_id

    buyer = call_action(mod.list_intercompany_invoices, conn, ns(
        company_id=menv["company_id"], limit="20", offset="0"))
    assert is_ok(buyer), buyer
    assert buyer["total"] == 1
    brow = buyer["invoices"][0]
    assert brow["direction"] == "purchase"
    assert brow["status"] == "draft"
    assert brow["supplier_name"] == "Seller As Supplier"


# ── 5. cancel reverses the ledger and deletes the draft mirror ──────────────

_EXPECTED_GL = sorted([
    ("Accounts Receivable", "0.00", "1000.00", 1, POSTING_DATE),
    ("Accounts Receivable", "1000.00", "0.00", 1, POSTING_DATE),
    ("COGS", "0.00", "100.00", 1, POSTING_DATE),
    ("COGS", "100.00", "0.00", 1, POSTING_DATE),
    ("Sales Revenue", "0.00", "1000.00", 1, POSTING_DATE),
    ("Sales Revenue", "1000.00", "0.00", 1, POSTING_DATE),
    ("Stock In Hand", "0.00", "100.00", 1, POSTING_DATE),
    ("Stock In Hand", "100.00", "0.00", 1, POSTING_DATE),
])


def test_cancel_reverses_ledger_and_deletes_draft_mirror(conn, env):
    menv, si_id, pi_id = _prepare(conn, env)
    result = call_action(mod.cancel_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(result), result
    assert result["si_gl_reversals"] == 4
    assert result["si_sle_reversals"] == 1
    assert result["pi_gl_reversals"] == 0
    assert result["pi_sle_reversals"] == 0
    assert _invoice_gl(conn, si_id) == _EXPECTED_GL
    assert _invoice_sle(conn, si_id) == [("-10.00", 1), ("10.00", 1)]
    assert _row(conn, "purchase_invoice", pi_id) is None
    pii = Table("purchase_invoice_item")
    leftovers = conn.execute(
        Q.from_(pii).select(pii.star)
        .where(pii.purchase_invoice_id == P()).get_sql(),
        (pi_id,)).fetchall()
    assert leftovers == []


# ── 6. a re-cancel is refused and writes nothing ────────────────────────────

def test_recancel_is_refused_and_writes_nothing(conn, env):
    _, si_id, _ = _prepare(conn, env)
    first = call_action(mod.cancel_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(first), first
    before = _snapshot(conn)
    refused = call_action(mod.cancel_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_error(refused)
    assert refused["message"] == \
        "Sales invoice %s is already cancelled; nothing was written" % si_id
    assert _snapshot(conn) == before


# ── 7. a failed stock reversal refuses and rolls back ───────────────────────

def test_failed_stock_reversal_refuses_and_rolls_back(conn, env):
    _, si_id, _ = _prepare(conn, env)
    before = _snapshot(conn)
    real_reverse = mod.reverse_sle_entries

    def _fail_after_real(conn_arg, voucher_type, voucher_id, posting_date):
        real_reverse(conn_arg, voucher_type, voucher_id, posting_date)
        raise ValueError("planted SLE reversal failure")

    with mock.patch.object(mod, "reverse_sle_entries",
                           side_effect=_fail_after_real):
        result = call_action(mod.cancel_intercompany_invoice, conn, ns(
            sales_invoice_id=si_id))
    assert is_error(result), result
    assert result["message"] == \
        "SLE reversal failed: planted SLE reversal failure"
    assert _snapshot(conn) == before
    assert _row(conn, "sales_invoice", si_id)["status"] == "submitted"


# ── 8. a stock-flagged mirror without rows still cancels ────────────────────

def test_mirror_marked_stock_without_rows_cancels(conn, env):
    menv, si_id, pi_id = _prepare(conn, env)
    submitted = call_action(buy.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id))
    assert is_ok(submitted), submitted
    pi = Table("purchase_invoice")
    conn.execute(
        Q.update(pi).set("update_stock", 1)
        .where(pi.id == P()).get_sql(), (pi_id,))
    conn.commit()
    result = call_action(mod.cancel_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(result), result
    assert result["pi_sle_reversals"] == 0
    assert _row(conn, "purchase_invoice", pi_id)["status"] == "cancelled"


# ── 9. cancel finishes a mirror left open by cancel-sales-invoice ────────────

def test_cancel_finishes_mirror_left_open_by_cancel_sales_invoice(conn, env):
    menv, si_id, pi_id = _prepare(conn, env)
    submitted = call_action(buy.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id))
    assert is_ok(submitted), submitted
    plain = call_action(mod.cancel_sales_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(plain), plain

    source_gl_before = _invoice_gl(conn, si_id)
    result = call_action(mod.cancel_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(result), result
    assert result["si_gl_reversals"] == 0
    assert result["si_sle_reversals"] == 0
    assert result["pi_gl_reversals"] == 2
    bill = _row(conn, "purchase_invoice", pi_id)
    assert bill["status"] == "cancelled"
    assert bill["outstanding_amount"] == "0"
    assert _invoice_gl(conn, si_id) == source_gl_before

    before = _snapshot(conn)
    refused = call_action(mod.cancel_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_error(refused)
    assert refused["message"] == \
        "Sales invoice %s is already cancelled; nothing was written" % si_id
    assert _snapshot(conn) == before
