"""Depth tests for 7 erpclaw-selling actions (m484-depth-erpclaw-selling-1).

Every test below reads the database back after the call and asserts exact
stored values. None of the 7 actions had a behavioural test before this file:
a search of this directory shows zero references to any of them (the only
mention is the word ``update-sales-invoice`` in test_delivery_invoice.py's
module docstring, with no corresponding test class).

Per-action depth (stored row vs ledger effect):
  - update-sales-invoice ......... STORED ROW (sales_invoice + items).
    Draft-only edit: reaches no ledger by design; the tests pin zero
    gl_entry / stock_ledger_entry / payment_ledger_entry rows for the voucher.
  - import-customers ............. FINDING: cannot execute (see below).
    Intended depth is STORED ROW (customer); the action never reaches
    the ledger, which the tests pin as a comment + zero-gl assertion.
  - list-dunning-runs ............ STORED ROW (dunning_run read back
    field-for-field against the table). Read-only: pins no-write.
  - add-intercompany-account-map . FINDING: cannot execute (see below).
    Intended depth is STORED ROW (intercompany_account_map).
  - list-intercompany-account-maps FINDING: cannot execute (see below).
    Intended depth is STORED ROW listing.
  - list-intercompany-invoices ... FINDING: cannot execute (see below).
    Intended depth is STORED ROW listing.
  - cancel-intercompany-invoice .. FINDING: cannot execute (see below).
    Intended depth is STORED ROW (status flips) + LEDGER (reversal legs
    balance per voucher).

FINDINGs (documented, deliberately not fixed -- schema changes are out of
scope for this task):
  F1 import-customers always raises
     ``ValueError: Unknown entity type 'customer'`` because
     ``get_next_name(conn, "customer")`` names an entity type absent from
     ``erpclaw_lib.naming.ENTITY_PREFIXES``. No CSV can ever be imported.
  F2 (CLOSED by m677): add/list-intercompany-account-map,
     list-intercompany-invoices and cancel-intercompany-invoice now run:
     init_schema creates the ``intercompany_account_map`` table and the
     ``is_intercompany`` / ``intercompany_reference_id`` columns on both
     invoice tables (migration 044 backfills existing installs), so the four
     actions reach their stored-row and ledger effects. The finding tests
     below are deleted and the intended tests run live.

Conventions: money is TEXT in the DB; every monetary assertion compares
exact Decimal values (never float, never round). Reads are plain SELECTs on
data tables only -- no catalog, no PRAGMA, no information_schema.
"""
import json

import pytest

from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_account, seed_customer, seed_supplier,
)
from erpclaw_lib.query import Q, Table

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r,
         "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _snapshot(conn, tables):
    """Dump whole data tables for byte-identical before/after comparison.

    Table names are hardcoded constants below; this issues no catalog query.
    """
    snap = {}
    for name in tables:
        t = Table(name)
        rows = conn.execute(
            Q.from_(t).select(t.star).orderby(t.id).get_sql(),
        ).fetchall()
        snap[name] = [dict(r) for r in rows]
    return snap


def _dec(value):
    return Decimal(str(value))


_SI_TABLES = ("sales_invoice", "sales_invoice_item", "gl_entry",
              "stock_ledger_entry", "payment_ledger_entry", "audit_log")


def _create_draft_si(conn, env, specs=(("item1", "5", "100.00"),),
                     posting_date="2026-06-20", due_date="2026-07-20"):
    result = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date=posting_date, due_date=due_date,
        items=_items(env, *specs), tax_template_id=None,
        payment_terms_id=None,
    ))
    assert is_ok(result), "setup: create_sales_invoice failed: %s" % (result,)
    return result["sales_invoice_id"]


def _gl_sums(conn, voucher_id):
    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE voucher_id=?",
        (voucher_id,)).fetchall()
    debits = sum((_dec(r["debit"]) for r in rows), Decimal("0"))
    credits = sum((_dec(r["credit"]) for r in rows), Decimal("0"))
    return len(rows), debits, credits


# ──────────────────────────────────────────────────────────────────────────────
# update-sales-invoice (WORKS -- stored row)
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateSalesInvoiceDepth:
    def test_due_date_change_persists_exact_row(self, conn, env):
        """Due-date edit rewrites exactly that column; nothing else moves."""
        si_a = _create_draft_si(conn, env)
        si_b = _create_draft_si(conn, env)
        before_b = dict(conn.execute(
            "SELECT * FROM sales_invoice WHERE id=?", (si_b,)).fetchone())

        result = call_action(mod.update_sales_invoice, conn, ns(
            sales_invoice_id=si_a, due_date="2026-08-15", items=None,
        ))
        assert is_ok(result)
        assert result["updated_fields"] == ["due_date"]

        row = dict(conn.execute(
            "SELECT * FROM sales_invoice WHERE id=?", (si_a,)).fetchone())
        assert row["due_date"] == "2026-08-15"
        assert row["status"] == "draft"
        assert row["total_amount"] == "500.00"
        assert _dec(row["total_amount"]) == Decimal("500.00")
        assert _dec(row["grand_total"]) == Decimal("500.00")
        assert _dec(row["outstanding_amount"]) == Decimal("500.00")
        assert row["outstanding_amount"] == row["grand_total"]
        assert row["posting_date"] == "2026-06-20"
        assert row["customer_id"] == env["customer"]
        assert row["company_id"] == env["company_id"]

        items = conn.execute(
            "SELECT * FROM sales_invoice_item WHERE sales_invoice_id=?",
            (si_a,)).fetchall()
        assert len(items) == 1
        assert _dec(items[0]["quantity"]) == Decimal("5")
        assert _dec(items[0]["rate"]) == Decimal("100.00")

        after_b = dict(conn.execute(
            "SELECT * FROM sales_invoice WHERE id=?", (si_b,)).fetchone())
        assert after_b == before_b

    def test_items_replace_recomputes_totals(self, conn, env):
        """New items replace old rows; totals/outstanding recompute exactly.

        A draft edit reaches no ledger by design, so this also pins zero
        gl_entry / stock_ledger_entry / payment_ledger_entry rows for the
        voucher rather than asserting legs that cannot exist.
        """
        si = _create_draft_si(conn, env)
        old_ids = sorted(r["id"] for r in conn.execute(
            "SELECT id FROM sales_invoice_item WHERE sales_invoice_id=?",
            (si,)).fetchall())
        assert len(old_ids) == 1

        new_items = json.dumps([
            {"item_id": env["item1"], "qty": "2", "rate": "100.00"},
            {"item_id": env["item2"], "qty": "1", "rate": "50.00"},
        ])
        result = call_action(mod.update_sales_invoice, conn, ns(
            sales_invoice_id=si, due_date=None, items=new_items,
        ))
        assert is_ok(result)
        assert result["updated_fields"] == [
            "items", "total_amount", "tax_amount", "grand_total"]

        row = dict(conn.execute(
            "SELECT * FROM sales_invoice WHERE id=?", (si,)).fetchone())
        assert row["total_amount"] == "250.00"
        assert row["grand_total"] == "250.00"
        assert row["outstanding_amount"] == "250.00"
        assert _dec(row["grand_total"]) == Decimal("250.00")
        assert _dec(row["outstanding_amount"]) == Decimal("250.00")
        assert _dec(row["tax_amount"]) == Decimal("0")
        assert row["status"] == "draft"

        # rate/quantity are TEXT: order numerically in Python, since
        # SQL text ordering would rank "50.00" above "100.00".
        fetched = conn.execute(
            "SELECT * FROM sales_invoice_item WHERE sales_invoice_id=?",
            (si,)).fetchall()
        assert len(fetched) == 2
        items = sorted(fetched, key=lambda r: _dec(r["rate"]), reverse=True)
        assert sorted(r["id"] for r in items) != old_ids
        assert _dec(items[0]["quantity"]) == Decimal("2")
        assert _dec(items[0]["rate"]) == Decimal("100.00")
        assert _dec(items[0]["amount"]) == Decimal("200.00")
        assert _dec(items[0]["net_amount"]) == Decimal("200.00")
        assert _dec(items[1]["quantity"]) == Decimal("1")
        assert _dec(items[1]["rate"]) == Decimal("50.00")
        assert _dec(items[1]["amount"]) == Decimal("50.00")
        assert _dec(items[1]["net_amount"]) == Decimal("50.00")

        assert _gl_sums(conn, si) == (0, Decimal("0"), Decimal("0"))
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM stock_ledger_entry "
            "WHERE voucher_id=?", (si,)).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM payment_ledger_entry "
            "WHERE voucher_id=?", (si,)).fetchone()["c"] == 0

    def test_refusal_submitted_invoice_leaves_db_identical(self, conn, env):
        """Updating a submitted invoice is refused; the DB is byte-identical."""
        si = _create_draft_si(conn, env)
        submit = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=si))
        assert is_ok(submit)

        # Submit posts both legs of the sale (AR DR 500.00 / revenue CR
        # 500.00) and, because update_stock=1, the perpetual-inventory pair
        # (COGS DR 50.00 / stock CR 50.00 for 5 units at the 10.00 seed
        # valuation rate): debits equal credits at 550.00.
        count, debits, credits = _gl_sums(conn, si)
        assert count == 4
        assert debits == credits
        assert debits == Decimal("550.00")
        legs = conn.execute(
            "SELECT debit, credit FROM gl_entry WHERE voucher_id=?",
            (si,)).fetchall()
        assert any(_dec(r["debit"]) == Decimal("500.00") for r in legs)
        assert any(_dec(r["credit"]) == Decimal("500.00") for r in legs)

        before = _snapshot(conn, _SI_TABLES)
        result = call_action(mod.update_sales_invoice, conn, ns(
            sales_invoice_id=si, due_date="2026-08-15", items=None,
        ))
        assert is_error(result)
        assert "submitted" in result["message"]
        assert "draft" in result["message"]
        assert _snapshot(conn, _SI_TABLES) == before


# ──────────────────────────────────────────────────────────────────────────────
# import-customers (BROKEN -- FINDING F1; intended depth: stored row)
# ──────────────────────────────────────────────────────────────────────────────

class TestImportCustomersDepth:
    def test_valid_csv_creates_the_customer(self, conn, env, tmp_path):
        """A valid CSV creates the customer with its type normalised."""
        csv_file = tmp_path / "customers.csv"
        csv_file.write_text(
            "name,customer_type,territory,default_currency,email,phone\n"
            "Acme Import,Company,East,USD,acme@example.com,555-0100\n"
        )
        customers_before = conn.execute(
            "SELECT COUNT(*) AS c FROM customer").fetchone()["c"]

        result = call_action(mod.import_customers, conn, ns(
            csv_path=str(csv_file), company_id=env["company_id"]))
        assert is_ok(result), result
        assert result["imported"] == 1
        stored = conn.execute(
            "SELECT * FROM customer WHERE name=?",
            ("Acme Import",)).fetchone()
        assert stored is not None
        assert stored["customer_type"] == "company"
        assert stored["territory"] == "East"
        assert stored["default_currency"] == "USD"
        assert stored["email"] == "acme@example.com"
        assert stored["phone"] == "555-0100"
        assert stored["company_id"] == env["company_id"]
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM customer").fetchone()["c"] \
            == customers_before + 1

    def test_refusal_missing_csv_path_writes_nothing(self, conn, env):
        """Missing --csv-path is refused before any file or DB touch."""
        before = _snapshot(conn, ("customer", "naming_series", "audit_log"))
        result = call_action(mod.import_customers, conn, ns(
            csv_path=None, company_id=env["company_id"]))
        assert is_error(result)
        assert result["message"] == "--csv-path is required"
        assert _snapshot(conn, ("customer", "naming_series", "audit_log")) \
            == before

    def test_refusal_nonexistent_file_writes_nothing(self, conn, env,
                                                     tmp_path):
        """A .csv path that names no file is refused truthfully, no writes."""
        missing = str(tmp_path / "no-such-file.csv")
        before = _snapshot(conn, ("customer", "naming_series", "audit_log"))
        result = call_action(mod.import_customers, conn, ns(
            csv_path=missing, company_id=env["company_id"]))
        assert is_error(result)
        assert "File not found" in result["message"]
        assert missing in result["message"]
        assert _snapshot(conn, ("customer", "naming_series", "audit_log")) \
            == before

    @pytest.mark.xfail(
        reason="FINDING F1: get_next_name has no 'customer' series, so the "
               "import raises before storing anything. Convert to a live "
               "stored-row test once the naming path exists.",
        strict=False)
    def test_intended_import_stores_exact_rows(self, conn, env, tmp_path):
        """INTENDED behaviour (currently unreachable): rows stored exactly.

        Two fresh names import; a duplicate name skips; every stored column
        reads back verbatim. The action never reaches the ledger, so this
        pins zero gl_entry rows rather than legs that cannot exist.
        """
        seed_customer(conn, env["company_id"], "Existing Customer")
        csv_file = tmp_path / "customers.csv"
        csv_file.write_text(
            "name,customer_type,territory,default_currency,email,phone\n"
            "Acme Import,Company,East,USD,acme@example.com,555-0100\n"
            "Beta Import,Company,West,USD,beta@example.com,555-0200\n"
            "Existing Customer,Company,East,USD,dup@example.com,555-0300\n"
        )
        result = call_action(mod.import_customers, conn, ns(
            csv_path=str(csv_file), company_id=env["company_id"]))
        assert is_ok(result)
        assert result["imported"] == 2
        assert result["skipped"] == 1
        assert result["total_rows"] == 3

        acme = conn.execute(
            "SELECT * FROM customer WHERE name=?",
            ("Acme Import",)).fetchone()
        assert acme is not None
        assert acme["customer_type"] == "Company"
        assert acme["territory"] == "East"
        assert acme["default_currency"] == "USD"
        assert acme["email"] == "acme@example.com"
        assert acme["phone"] == "555-0100"
        assert acme["company_id"] == env["company_id"]
        assert acme["naming_series"]

        beta = conn.execute(
            "SELECT * FROM customer WHERE name=?",
            ("Beta Import",)).fetchone()
        assert beta is not None
        assert beta["email"] == "beta@example.com"

        dupes = conn.execute(
            "SELECT * FROM customer WHERE name=?",
            ("Existing Customer",)).fetchall()
        assert len(dupes) == 1
        assert dupes[0]["email"] != "dup@example.com"

        assert conn.execute(
            "SELECT COUNT(*) AS c FROM gl_entry").fetchone()["c"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# list-dunning-runs (WORKS -- stored row, read-only)
# ──────────────────────────────────────────────────────────────────────────────

def _seed_overdue_run(conn, env, run_date="2026-10-01"):
    """Level + overdue submitted invoice + cycle; returns (si_id, run_id)."""
    level = call_action(mod.add_dunning_level, conn, ns(
        company_id=env["company_id"], level=1, days_overdue=30,
        dunning_action="call", template_id=None, description=None,
    ))
    assert is_ok(level), "setup: add_dunning_level failed: %s" % (level,)
    si = _create_draft_si(conn, env, posting_date="2026-01-05",
                          due_date="2026-02-01")
    submit = call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=si))
    assert is_ok(submit), "setup: submit failed: %s" % (submit,)
    cycle = call_action(mod.run_dunning_cycle, conn, ns(
        company_id=env["company_id"], run_date=run_date))
    assert is_ok(cycle), "setup: run_dunning_cycle failed: %s" % (cycle,)
    assert cycle["runs_created"] == 1
    return si, cycle["run_ids"][0]


class TestListDunningRunsDepth:
    def test_lists_created_run_with_exact_stored_values(self, conn, env):
        """Every field the list returns equals the stored dunning_run row."""
        si, run_id = _seed_overdue_run(conn, env)

        result = call_action(mod.list_dunning_runs, conn, ns(
            customer_id=None, company_id=env["company_id"], limit=None))
        assert is_ok(result)
        assert len(result["runs"]) == 1

        stored = dict(conn.execute(
            "SELECT * FROM dunning_run WHERE id=?", (run_id,)).fetchone())
        listed = result["runs"][0]
        assert listed["id"] == run_id
        assert listed["company_id"] == env["company_id"] == \
            stored["company_id"]
        assert listed["run_date"] == "2026-10-01" == stored["run_date"]
        assert listed["customer_id"] == env["customer"] == \
            stored["customer_id"]
        assert listed["level"] == 1 == stored["level"]
        assert listed["action_taken"] == "call" == stored["action_taken"]
        assert listed["status"] == "completed" == stored["status"]
        assert json.loads(listed["invoice_ids_json"]) == [si]
        assert json.loads(stored["invoice_ids_json"]) == [si]

    def test_filters_narrow_and_listing_writes_nothing(self, conn, env):
        """Customer filter narrows; unknown scope refuses; no rows written.

        The list is read-only by design, so instead of ledger legs this pins
        the dunning tables byte-identical across all three listings.
        """
        si, run_id = _seed_overdue_run(conn, env)
        other = seed_customer(conn, env["company_id"], "Quiet Customer")
        tables = ("dunning_run", "dunning_level", "customer",
                  "sales_invoice")
        before = _snapshot(conn, tables)

        by_customer = call_action(mod.list_dunning_runs, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            limit=None))
        assert is_ok(by_customer)
        assert len(by_customer["runs"]) == 1
        assert by_customer["runs"][0]["id"] == run_id

        by_other = call_action(mod.list_dunning_runs, conn, ns(
            customer_id=other, company_id=env["company_id"], limit=None))
        assert is_ok(by_other)
        assert by_other["runs"] == []

        by_unknown = call_action(mod.list_dunning_runs, conn, ns(
            customer_id=None, company_id="no-such-company", limit=None))
        assert by_unknown == {"status": "error",
                              "error": "Company not found: no-such-company",
                              "message": "Company not found: no-such-company"}

        assert _snapshot(conn, tables) == before
        assert si is not None

    def test_invalid_limit_is_ignored_without_error(self, conn, env):
        """No input validation exists on this action (all filters optional),
        so a non-numeric limit is silently ignored and every run returns.
        Documented here so a later reader does not invent a refusal case
        the code cannot produce; the listing still writes nothing.
        """
        _, run_id = _seed_overdue_run(conn, env)
        before = _snapshot(conn, ("dunning_run",))
        result = call_action(mod.list_dunning_runs, conn, ns(
            customer_id=None, company_id=env["company_id"],
            limit="not-a-number"))
        assert is_ok(result)
        assert len(result["runs"]) == 1
        assert result["runs"][0]["id"] == run_id
        assert _snapshot(conn, ("dunning_run",)) == before


# ──────────────────────────────────────────────────────────────────────────────
# add-intercompany-account-map (BROKEN -- FINDING F2; intended: stored row)
# ──────────────────────────────────────────────────────────────────────────────

def _second_company_with_accounts(conn):
    company_b = seed_company(conn, "Second Co", "SC")
    revenue_b = seed_account(conn, company_b, "Second Revenue", "income",
                             "revenue", "4000")
    return company_b, revenue_b


class TestAddIntercompanyAccountMapDepth:
    def test_refusal_same_company_writes_nothing(self, conn, env):
        """Same source/target company is refused before any DB touch."""
        before = _snapshot(conn, ("company", "account"))
        result = call_action(mod.add_intercompany_account_map, conn, ns(
            company_id=env["company_id"],
            target_company_id=env["company_id"],
            source_account_id=env["revenue"],
            target_account_id=env["revenue"],
        ))
        assert is_error(result)
        assert result["message"] == \
            "Source and target company must be different"
        assert _snapshot(conn, ("company", "account")) == before

    def test_refusal_missing_ids_writes_nothing(self, conn, env):
        """Missing source account is refused before any DB touch."""
        before = _snapshot(conn, ("company", "account"))
        result = call_action(mod.add_intercompany_account_map, conn, ns(
            company_id=env["company_id"], target_company_id=None,
            source_account_id=None, target_account_id=None,
        ))
        assert is_error(result)
        assert "required" in result["message"]
        assert _snapshot(conn, ("company", "account")) == before

    def test_intended_map_row_stored_exact(self, conn, env):
        """The mapping row is stored with exactly the four ids supplied
        (a stored-row effect; the action never reaches the ledger)."""
        company_b, revenue_b = _second_company_with_accounts(conn)
        result = call_action(mod.add_intercompany_account_map, conn, ns(
            company_id=env["company_id"], target_company_id=company_b,
            source_account_id=env["revenue"],
            target_account_id=revenue_b,
        ))
        assert is_ok(result)
        stored = conn.execute(
            "SELECT * FROM intercompany_account_map WHERE id=?",
            (result["map_id"],)).fetchone()
        assert stored is not None
        assert stored["source_company_id"] == env["company_id"]
        assert stored["target_company_id"] == company_b
        assert stored["source_account_id"] == env["revenue"]
        assert stored["target_account_id"] == revenue_b


# ──────────────────────────────────────────────────────────────────────────────
# list-intercompany-account-maps (BROKEN -- FINDING F2; intended: stored rows)
# ──────────────────────────────────────────────────────────────────────────────

class TestListIntercompanyAccountMapsDepth:
    def test_refusal_missing_company_writes_nothing(self, conn, env):
        """Missing --company-id is refused before any DB touch."""
        before = _snapshot(conn, ("company", "account"))
        result = call_action(mod.list_intercompany_account_maps, conn, ns(
            company_id=None, target_company_id=None))
        assert is_error(result)
        assert result["message"] == "--company-id (source company) is required"
        assert _snapshot(conn, ("company", "account")) == before

    def test_intended_list_returns_stored_mapping(self, conn, env):
        """After a stored map, the listing returns it with exact ids and
        account names, and the target-company filter narrows to it."""
        company_b, revenue_b = _second_company_with_accounts(conn)
        added = call_action(mod.add_intercompany_account_map, conn, ns(
            company_id=env["company_id"], target_company_id=company_b,
            source_account_id=env["revenue"],
            target_account_id=revenue_b,
        ))
        assert is_ok(added)
        result = call_action(mod.list_intercompany_account_maps, conn, ns(
            company_id=env["company_id"], target_company_id=company_b))
        assert is_ok(result)
        assert result["total"] == 1
        mapping = result["mappings"][0]
        assert mapping["id"] == added["map_id"]
        assert mapping["source_account_id"] == env["revenue"]
        assert mapping["target_account_id"] == revenue_b


# ──────────────────────────────────────────────────────────────────────────────
# list-intercompany-invoices (BROKEN -- FINDING F2; intended: stored rows)
# ──────────────────────────────────────────────────────────────────────────────

class TestListIntercompanyInvoicesDepth:
    def test_refusal_missing_company_writes_nothing(self, conn, env):
        """Missing --company-id is refused before any DB touch."""
        tables = ("sales_invoice", "purchase_invoice")
        before = _snapshot(conn, tables)
        result = call_action(mod.list_intercompany_invoices, conn, ns(
            company_id=None, limit="20", offset="0"))
        assert is_error(result)
        assert result["message"] == "--company-id is required"
        assert _snapshot(conn, tables) == before

    def test_intended_list_returns_mirrored_pair(self, conn, env):
        """After mirroring, the source list shows the sales leg
        (direction 'sales') with the exact stored grand total, and the
        listing itself posts no ledger rows."""
        company_b, _ = _second_company_with_accounts(conn)
        supplier_b = seed_supplier(conn, company_b, "Source Co Supplier")
        si = _create_draft_si(conn, env, posting_date="2026-01-05",
                              due_date="2026-02-01")
        submit = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=si))
        assert is_ok(submit)
        mirror = call_action(mod.create_intercompany_invoice, conn, ns(
            sales_invoice_id=si, target_company_id=company_b,
            supplier_id=supplier_b))
        assert is_ok(mirror)
        result = call_action(mod.list_intercompany_invoices, conn, ns(
            company_id=env["company_id"], limit="20", offset="0"))
        assert is_ok(result)
        assert result["total"] == 1
        assert result["invoices"][0]["direction"] == "sales"
        assert _dec(result["invoices"][0]["grand_total"]) == \
            Decimal("500.00")


# ──────────────────────────────────────────────────────────────────────────────
# cancel-intercompany-invoice (BROKEN -- FINDING F2; intended: row + ledger)
# ──────────────────────────────────────────────────────────────────────────────

class TestCancelIntercompanyInvoiceDepth:
    def test_refusal_unknown_invoice_writes_nothing(self, conn, env):
        """An unknown id is refused truthfully before any DB touch."""
        tables = ("sales_invoice", "purchase_invoice")
        before = _snapshot(conn, tables)
        result = call_action(mod.cancel_intercompany_invoice, conn, ns(
            sales_invoice_id="does-not-exist"))
        assert is_error(result)
        assert result["message"] == "Sales invoice not found: does-not-exist"
        assert _snapshot(conn, tables) == before

    def test_refusal_missing_id_writes_nothing(self, conn, env):
        """A missing id is refused before any DB touch."""
        tables = ("sales_invoice", "purchase_invoice")
        before = _snapshot(conn, tables)
        result = call_action(mod.cancel_intercompany_invoice, conn, ns(
            sales_invoice_id=None))
        assert is_error(result)
        assert result["message"] == "--sales-invoice-id is required"
        assert _snapshot(conn, tables) == before

    def test_intended_cancel_reverses_gl_and_deletes_draft_mirror(self, conn, env):
        """Cancelling flips the sales leg to cancelled with zero
        outstanding, posts reversal GL whose legs balance per voucher
        (sum of debits equals sum of credits across original + reversal),
        and deletes the draft mirror bill by design."""
        company_b, _ = _second_company_with_accounts(conn)
        supplier_b = seed_supplier(conn, company_b, "Source Co Supplier")
        si = _create_draft_si(conn, env, posting_date="2026-01-05",
                              due_date="2026-02-01")
        assert is_ok(call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=si)))
        mirror = call_action(mod.create_intercompany_invoice, conn, ns(
            sales_invoice_id=si, target_company_id=company_b,
            supplier_id=supplier_b))
        assert is_ok(mirror)
        result = call_action(mod.cancel_intercompany_invoice, conn, ns(
            sales_invoice_id=si))
        assert is_ok(result)
        assert result["si_status"] == "cancelled"
        si_row = dict(conn.execute(
            "SELECT * FROM sales_invoice WHERE id=?", (si,)).fetchone())
        assert si_row["status"] == "cancelled"
        assert _dec(si_row["outstanding_amount"]) == Decimal("0")
        assert conn.execute(
            "SELECT * FROM purchase_invoice WHERE id=?",
            (result["purchase_invoice_id"],)).fetchone() is None
        _, debits, credits = _gl_sums(conn, si)
        assert debits == credits
