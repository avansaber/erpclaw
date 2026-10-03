"""m323 — cancel-intercompany-invoice: prove the ledger reversal, stop swallowing its failure.

Covers ``cancel-intercompany-invoice`` (erpclaw-selling), the last money-touching
action whose test proved only that it dispatched.

Schema precondition (read before touching this file): the foundation schema in
this tree declares NEITHER ``sales_invoice.is_intercompany`` /
``.intercompany_reference_id`` (nor the ``purchase_invoice`` twins) NOR the
``intercompany_account_map`` table the ``create-intercompany-invoice`` action
reads. ``_ensure_intercompany_linkage`` therefore ensures the four linkage
columns with portable ``ALTER TABLE ... ADD COLUMN`` after asking
``erpclaw_lib.seam`` what exists (no catalog literals). The mirror bill is
linked directly and submitted through the REAL buying ``submit-purchase-invoice``
action; ``create-intercompany-invoice`` itself is not driven because it SELECTs
the missing map table and cannot run until the foundation declares it — a schema
gap for the foundation owner, out of scope for this task.

Tests:
  1. ``test_cancel_reverses_both_legs_and_leaves_third_company_untouched`` —
     happy path over two submitted legs plus an uninvolved third company.
  2. ``test_cancel_with_draft_mirror_deletes_mirror_and_reverses_source`` —
     draft mirror is deleted, submitted mirror is reversed.
  3. ``test_planted_reversal_failure_refuses_and_rolls_back`` — PLANTED-FAILURE
     PROOF (m323): the ledger reversal raises mid-cancel. On pre-fix code the
     ``ValueError`` is swallowed and the invoice is silently marked cancelled
     with no reversal, so this test FAILS there; after the fix the action rolls
     back and refuses, so it PASSES. Left in place as the proof the fix is
     needed.

Money is text throughout: ``Decimal`` in Python, exact string comparisons.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal
from unittest import mock

import pytest

from selling_helpers import (
    build_selling_env,
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


# The mirror leg is submitted through the real buying action (same idiom the
# allocation-release suite uses), so no test-side re-implementation creeps in.
buy = _load("db_query_buying_m323", "erpclaw-buying/db_query.py")

from erpclaw_lib import seam  # noqa: E402  (selling_helpers binds the tree lib first)

D = Decimal
POSTING_DATE = "2026-06-20"
DUE_DATE = "2026-07-20"


# ── fixtures ────────────────────────────────────────────────────────────────

def _ensure_intercompany_linkage(conn, db_path):
    """Ensure the four intercompany linkage columns exist.

    Asks the seam what exists; adds what is missing with portable
    ``ADD COLUMN``. Never touches values, never creates tables.
    """
    for table in ("sales_invoice", "purchase_invoice"):
        cols = seam.column_names(table, db_path)
        if "is_intercompany" not in cols:
            conn.execute(
                "ALTER TABLE " + table
                + " ADD COLUMN is_intercompany INTEGER NOT NULL DEFAULT 0"
            )
        if "intercompany_reference_id" not in cols:
            conn.execute(
                "ALTER TABLE " + table + " ADD COLUMN intercompany_reference_id TEXT"
            )
    conn.commit()


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


def _submit_si(conn, env, qty="10", rate="100.00"):
    """Real create + submit of a stock sales invoice in company A."""
    items = json.dumps([{
        "item_id": env["item1"], "qty": qty, "rate": rate,
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


def _insert_draft_mirror(conn, si_id, menv, item_id, qty="10", rate="100.00"):
    """Insert the draft mirror bill in company B and link both sides.

    Mirrors what ``create-intercompany-invoice`` records (draft,
    ``update_stock = 0``, linkage flags on both documents) without SELECTing
    the foundation-missing account-map table; the posting itself always goes
    through the real buying submit below.
    """
    pi_id = str(uuid.uuid4())
    amount = str(D(qty) * D(rate))
    conn.execute(
        "INSERT INTO purchase_invoice (id, supplier_id, posting_date, due_date,"
        " currency, exchange_rate, total_amount, tax_amount, grand_total,"
        " outstanding_amount, status, update_stock, is_intercompany,"
        " intercompany_reference_id, company_id)"
        " VALUES (?, ?, ?, ?, 'USD', '1', ?, '0', ?, ?,"
        " 'draft', 0, 1, ?, ?)",
        (pi_id, menv["supplier"], POSTING_DATE, DUE_DATE,
         amount, amount, amount, si_id, menv["company_id"]),
    )
    conn.execute(
        "INSERT INTO purchase_invoice_item (id, purchase_invoice_id, item_id,"
        " quantity, uom, rate, amount, expense_account_id)"
        " VALUES (?, ?, ?, ?, 'Each', ?, ?, ?)",
        (str(uuid.uuid4()), pi_id, item_id, qty, rate, amount,
         menv["expense"]),
    )
    conn.execute(
        "UPDATE sales_invoice SET is_intercompany = 1,"
        " intercompany_reference_id = ? WHERE id = ?",
        (pi_id, si_id),
    )
    conn.commit()
    return pi_id


def _submit_mirror(conn, si_id, menv, item_id, qty="10", rate="100.00"):
    """Draft mirror bill linked to ``si_id``, submitted via real buying action."""
    pi_id = _insert_draft_mirror(conn, si_id, menv, item_id, qty, rate)
    submitted = call_action(buy.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id))
    assert is_ok(submitted), submitted
    return pi_id


# ── ledger readers (raw SELECTs; no catalog probes) ─────────────────────────

def _gl(conn, voucher_type, voucher_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_type = ? AND voucher_id = ?"
        " ORDER BY id", (voucher_type, voucher_id))]


def _sle(conn, voucher_type, voucher_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stock_ledger_entry WHERE voucher_type = ?"
        " AND voucher_id = ? ORDER BY id", (voucher_type, voucher_id))]


def _ple(conn, voucher_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM payment_ledger_entry WHERE voucher_id = ? ORDER BY id",
        (voucher_id,))]


def _doc(conn, table, doc_id):
    row = conn.execute(
        "SELECT * FROM " + table + " WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def _company_gl(conn, company_id):
    return [dict(r) for r in conn.execute(
        "SELECT g.* FROM gl_entry g JOIN account a ON a.id = g.account_id"
        " WHERE a.company_id = ? ORDER BY g.id", (company_id,))]


def _account_company(conn):
    return {r["id"]: r["company_id"] for r in conn.execute(
        "SELECT id, company_id FROM account")}


# ── shared assertions ───────────────────────────────────────────────────────

def _assert_full_gl_reversal(before, after):
    """Cancel means reverse, never update: originals untouched except the
    ``is_cancelled`` flag, one exact debit<->credit mirror per original, and a
    balanced leg overall."""
    assert before, "no original postings to reverse"
    assert len(after) == 2 * len(before)
    by_id = {row["id"]: row for row in before}
    assert set(by_id) < {row["id"] for row in after}
    reversals = [row for row in after if row["id"] not in by_id]
    assert len(reversals) == len(before)
    for orig in before:
        kept = next(row for row in after if row["id"] == orig["id"])
        for key, value in orig.items():
            if key == "is_cancelled":
                continue
            assert kept[key] == value, key
        assert kept["is_cancelled"] == 1
    unmatched = list(reversals)
    for orig in before:
        hit = next(
            (row for row in unmatched
             if row["account_id"] == orig["account_id"]
             and row["voucher_type"] == orig["voucher_type"]
             and row["voucher_id"] == orig["voucher_id"]
             and row["debit"] == orig["credit"]
             and row["credit"] == orig["debit"]),
            None,
        )
        assert hit is not None, orig
        unmatched.remove(hit)
    assert unmatched == []
    net = sum((D(row["debit"]) - D(row["credit"]) for row in after), D("0"))
    assert net == D("0")


# ── 1. happy path: both legs submitted, plus an uninvolved third company ────

def test_cancel_reverses_both_legs_and_leaves_third_company_untouched(
        conn, db_path, env):
    _ensure_intercompany_linkage(conn, db_path)
    menv = _mirror_env(conn)
    si_id = _submit_si(conn, env)
    pi_id = _submit_mirror(conn, si_id, menv, env["item1"])

    envC = build_selling_env(conn)
    siC = _submit_si(conn, envC)
    c_gl_before = _company_gl(conn, envC["company_id"])
    assert c_gl_before, "third company needs posted rows to prove untouched"
    c_doc_before = _doc(conn, "sales_invoice", siC)
    c_ple_before = _ple(conn, siC)

    si_gl_before = _gl(conn, "sales_invoice", si_id)
    pi_gl_before = _gl(conn, "purchase_invoice", pi_id)
    si_sle_before = _sle(conn, "sales_invoice", si_id)
    si_ple_before = _ple(conn, si_id)
    pi_ple_before = _ple(conn, pi_id)
    assert si_ple_before and all(row["delinked"] == 0 for row in si_ple_before)
    assert pi_ple_before and all(row["delinked"] == 0 for row in pi_ple_before)
    assert len(si_gl_before) == 4  # AR + revenue + COGS pair
    assert len(pi_gl_before) == 2  # expense + payable
    assert len(si_sle_before) == 1

    result = call_action(mod.cancel_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(result), result
    assert result["si_status"] == "cancelled"
    assert result["purchase_invoice_id"] == pi_id
    assert result["si_gl_reversals"] == 4
    assert result["si_sle_reversals"] == 1
    assert result["pi_gl_reversals"] == 2
    assert result["pi_sle_reversals"] == 0

    _assert_full_gl_reversal(si_gl_before, _gl(conn, "sales_invoice", si_id))
    _assert_full_gl_reversal(pi_gl_before, _gl(conn, "purchase_invoice", pi_id))

    # Source SLE: original kept with its quantities, flagged; one mirror with
    # the negated quantity.
    si_sle_after = _sle(conn, "sales_invoice", si_id)
    assert len(si_sle_after) == 2
    kept_sle = next(row for row in si_sle_after if row["id"] == si_sle_before[0]["id"])
    for key, value in si_sle_before[0].items():
        if key == "is_cancelled":
            continue
        assert kept_sle[key] == value, key
    assert kept_sle["is_cancelled"] == 1
    mirror_sle = next(row for row in si_sle_after
                      if row["id"] != si_sle_before[0]["id"])
    assert D(mirror_sle["actual_qty"]) == -D(si_sle_before[0]["actual_qty"])

    # Payment ledger: every row for both documents delinked, amounts kept.
    si_ple = _ple(conn, si_id)
    assert len(si_ple) == len(si_ple_before)
    assert all(row["delinked"] == 1 for row in si_ple)
    assert [row["amount"] for row in si_ple] == [
        row["amount"] for row in si_ple_before]
    pi_ple = _ple(conn, pi_id)
    assert len(pi_ple) == len(pi_ple_before)
    assert all(row["delinked"] == 1 for row in pi_ple)
    assert [row["amount"] for row in pi_ple] == [
        row["amount"] for row in pi_ple_before]

    si_doc = _doc(conn, "sales_invoice", si_id)
    assert si_doc["status"] == "cancelled"
    assert si_doc["outstanding_amount"] == "0"
    pi_doc = _doc(conn, "purchase_invoice", pi_id)
    assert pi_doc["status"] == "cancelled"
    assert pi_doc["outstanding_amount"] == "0"

    # Company scoping: new source rows sit on company A accounts, new mirror
    # rows on company B accounts, and company C is byte-identical.
    acct_company = _account_company(conn)
    si_new = [row for row in _gl(conn, "sales_invoice", si_id)
              if row["id"] not in {r["id"] for r in si_gl_before}]
    pi_new = [row for row in _gl(conn, "purchase_invoice", pi_id)
              if row["id"] not in {r["id"] for r in pi_gl_before}]
    assert si_new and all(
        acct_company[row["account_id"]] == env["company_id"] for row in si_new)
    assert pi_new and all(
        acct_company[row["account_id"]] == menv["company_id"] for row in pi_new)
    assert _company_gl(conn, envC["company_id"]) == c_gl_before
    assert _doc(conn, "sales_invoice", siC) == c_doc_before
    assert _ple(conn, siC) == c_ple_before


# ── 2. draft mirror is deleted, submitted mirror is reversed ────────────────

def test_cancel_with_draft_mirror_deletes_mirror_and_reverses_source(
        conn, db_path, env):
    _ensure_intercompany_linkage(conn, db_path)
    menv = _mirror_env(conn)
    si_id = _submit_si(conn, env)
    pi_id = _insert_draft_mirror(conn, si_id, menv, env["item1"])
    assert _doc(conn, "purchase_invoice", pi_id)["status"] == "draft"

    si_gl_before = _gl(conn, "sales_invoice", si_id)

    result = call_action(mod.cancel_intercompany_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(result), result

    assert _doc(conn, "purchase_invoice", pi_id) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM purchase_invoice_item"
        " WHERE purchase_invoice_id = ?", (pi_id,)).fetchone()[0] == 0

    _assert_full_gl_reversal(si_gl_before, _gl(conn, "sales_invoice", si_id))
    si_doc = _doc(conn, "sales_invoice", si_id)
    assert si_doc["status"] == "cancelled"
    assert si_doc["outstanding_amount"] == "0"
    assert all(row["delinked"] == 1 for row in _ple(conn, si_id))


# ── 3. PLANTED-FAILURE PROOF (m323): a failed reversal rolls everything back ─

def test_planted_reversal_failure_refuses_and_rolls_back(conn, db_path, env):
    """PLANTED-FAILURE PROOF (m323): the ledger reversal raises mid-cancel.

    Fails on pre-fix code, where the ``ValueError`` is swallowed and the action
    still marks the invoice cancelled with outstanding zeroed and no reversal.
    Passes after the fix, where the failed reversal rolls the whole action back
    and refuses with the reason. Kept as the proof the fix is needed.
    """
    _ensure_intercompany_linkage(conn, db_path)
    menv = _mirror_env(conn)
    si_id = _submit_si(conn, env)
    pi_id = _submit_mirror(conn, si_id, menv, env["item1"])

    si_doc_before = _doc(conn, "sales_invoice", si_id)
    pi_doc_before = _doc(conn, "purchase_invoice", pi_id)
    si_gl_count = len(_gl(conn, "sales_invoice", si_id))
    pi_gl_count = len(_gl(conn, "purchase_invoice", pi_id))
    assert si_doc_before["status"] == "submitted"
    assert pi_doc_before["status"] == "submitted"

    real_reverse = mod.reverse_gl_entries
    calls = []

    def _fail_once(conn_arg, voucher_type, voucher_id, posting_date):
        calls.append((voucher_type, voucher_id))
        if len(calls) == 1:
            raise ValueError(
                "No active GL entries found for voucher"
                " (%s, %s)" % (voucher_type, voucher_id)
            )
        return real_reverse(conn_arg, voucher_type, voucher_id, posting_date)

    with mock.patch.object(mod, "reverse_gl_entries", side_effect=_fail_once):
        result = call_action(mod.cancel_intercompany_invoice, conn, ns(
            sales_invoice_id=si_id))

    assert is_error(result), result
    assert "reversal" in result["message"].lower()

    # Committed state is unchanged: still submitted, outstanding kept to the
    # exact string, no partial reversal rows, nothing delinked.
    si_doc = _doc(conn, "sales_invoice", si_id)
    assert si_doc["status"] == "submitted"
    assert si_doc["outstanding_amount"] == si_doc_before["outstanding_amount"]
    pi_doc = _doc(conn, "purchase_invoice", pi_id)
    assert pi_doc["status"] == "submitted"
    assert pi_doc["outstanding_amount"] == pi_doc_before["outstanding_amount"]
    assert len(_gl(conn, "sales_invoice", si_id)) == si_gl_count
    assert len(_gl(conn, "purchase_invoice", pi_id)) == pi_gl_count
    assert all(row["is_cancelled"] == 0
               for row in _gl(conn, "sales_invoice", si_id))
    assert all(row["is_cancelled"] == 0
               for row in _gl(conn, "purchase_invoice", pi_id))
    assert all(row["delinked"] == 0 for row in _ple(conn, si_id))
    assert all(row["delinked"] == 0 for row in _ple(conn, pi_id))
