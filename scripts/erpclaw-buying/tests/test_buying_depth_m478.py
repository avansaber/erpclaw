"""Behavioural depth for ten buying actions (task m478).

Each action below previously had only a shape test (asserts on the response
envelope, e.g. ``"items" in result``) or a routability test (the contract
suite's ``"Unknown action" not in ...``). Neither observes the database, so an
action could return a perfect envelope while writing nothing and stay green.
Every test here reads the stored rows back with PyPika-built queries through
``erpclaw_lib.query`` and compares exact values; money is compared as exact
``Decimal`` strings, never float.

Per-action depth (stored row vs ledger effect):

- submit-rfq: stored row (RFQ status/naming + rfq_supplier sent dates + audit).
  Posts no ledger rows; the test asserts the ledgers are untouched.
- add-supplier-quotation: stored rows (quotation header + lines + rfq_supplier
  link + RFQ status flip). Posts no ledger rows; asserted untouched.
- compare-supplier-quotations: read-only. The test pins the comparison against
  the stored rates and proves no table changed. No ledger assertion can hold
  for a pure read, so none is made.
- create-debit-note: stored row (negative draft + lines; original bill
  byte-identical). A draft posts no ledger rows; asserted untouched. The
  submit-time mirror legs are covered by
  test_submitted_debit_note_posts_mirror_gl_and_negative_payable.
- get-purchase-order / get-purchase-receipt / get-purchase-invoice: read-only.
  Each test pins the response field-by-field against the stored rows and
  proves no table changed. No ledger assertions; reads post nothing.
- import-suppliers: DEFECT (see TestImportSuppliersDoc) — a valid import
  raises ``ValueError: Unknown entity type 'supplier'`` and writes nothing.
  The test documents that real behaviour and pins the validation refusals.
- update-purchase-invoice: stored row (header totals recomputed with tax +
  replaced lines; old lines gone). A draft update posts no ledger rows;
  asserted untouched.
- update-three-way-match-policy: stored row (company policy from strict to
  the new value + audit). Posts no ledger rows; asserted untouched.

No test in this file inspects catalog tables or sets connection
options; reads are PyPika-built and run on a connection from
``erpclaw_lib.db.get_connection``.
"""
import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from buying_helpers import (  # noqa: E402
    build_buying_env, call_action, is_error, is_ok, load_db_query, ns,
    seed_supplier,
)
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, fn, insert_row  # noqa: E402

B = load_db_query()

PO_DATE = "2026-06-15"
RECEIPT_DATE = "2026-06-20"
INVOICE_DATE = "2026-06-25"


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def env(conn):
    return build_buying_env(conn)


def _u():
    return str(uuid.uuid4())


def _msg(result):
    return result.get("message", "")


def _row(conn, table, row_id):
    t = Table(table)
    q = Q.from_(t).select(t.star).where(t.id == P())
    found = conn.execute(q.get_sql(), (row_id,)).fetchone()
    assert found is not None, f"{table} {row_id} not found"
    return dict(found)


def _where(conn, table, **filters):
    t = Table(table)
    q = Q.from_(t).select(t.star)
    params = []
    for column, value in filters.items():
        q = q.where(Field(column) == P())
        params.append(value)
    return [dict(r) for r in conn.execute(q.get_sql(), params).fetchall()]


def _all(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _count(conn, table):
    t = Table(table)
    q = Q.from_(t).select(fn.Count("*").as_("n"))
    return conn.execute(q.get_sql()).fetchone()["n"]


def _snapshot(conn, tables):
    return {name: _all(conn, name) for name in tables}


def _insert(conn, table, row):
    sql, _cols = insert_row(table, {key: P() for key in row})
    conn.execute(sql, tuple(row.values()))
    conn.commit()


def _gl(conn, voucher_type, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(t.account_id, t.debit, t.credit, t.party_type, t.party_id,
                 t.posting_date, t.voucher_type, t.voucher_id, t.is_cancelled)
         .where(t.voucher_type == P())
         .where(t.voucher_id == P()))
    return [dict(r) for r in
            conn.execute(q.get_sql(), (voucher_type, voucher_id)).fetchall()]


def _audit(conn, action, entity_id):
    rows = _where(conn, "audit_log", action=action, entity_id=entity_id)
    return [r for r in rows if r["skill"] == "erpclaw-buying"]


_LEDGERS = ("gl_entry", "payment_ledger_entry", "stock_ledger_entry")
_RFQ_TABLES = ("request_for_quotation", "rfq_item", "rfq_supplier",
               "supplier_quotation", "supplier_quotation_item",
               "naming_series", "audit_log")
_DOC_TABLES = ("purchase_order", "purchase_order_item",
               "purchase_receipt", "purchase_receipt_item",
               "purchase_invoice", "purchase_invoice_item",
               "gl_entry", "payment_ledger_entry", "stock_ledger_entry",
               "naming_series", "audit_log")


def _add_rfq(conn, env, suppliers=None):
    r = call_action(B.add_rfq, conn, ns(
        company_id=env["company_id"],
        items=json.dumps([{"item_id": env["item1"], "qty": "10"},
                          {"item_id": env["item2"], "qty": "4"}]),
        suppliers=json.dumps(suppliers if suppliers is not None
                             else [env["supplier"]])))
    assert is_ok(r), r
    return r["rfq_id"]


def _rfq_lines(conn, rfq_id):
    t = Table("rfq_item")
    q = Q.from_(t).select(t.id, t.item_id).where(t.rfq_id == P())
    return {r["item_id"]: r["id"]
            for r in conn.execute(q.get_sql(), (rfq_id,)).fetchall()}


def _quote(conn, rfq_id, supplier_id, line_a, rate_a, line_b, rate_b):
    r = call_action(B.add_supplier_quotation, conn, ns(
        rfq_id=rfq_id, supplier_id=supplier_id,
        items=json.dumps([{"rfq_item_id": line_a, "rate": rate_a},
                          {"rfq_item_id": line_b, "rate": rate_b}])))
    assert is_ok(r), r
    return r["supplier_quotation_id"]


def _confirmed_po(conn, env, lines=(("item1", "10", "50.00"),
                                    ("item2", "5", "20.00"))):
    r = call_action(B.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date=PO_DATE,
        items=json.dumps([{"item_id": env[key], "qty": qty, "rate": rate,
                           "warehouse_id": env["warehouse"]}
                          for key, qty, rate in lines]),
        tax_template_id=None, name=None))
    assert is_ok(r), r
    s = call_action(B.submit_purchase_order, conn, ns(
        purchase_order_id=r["purchase_order_id"]))
    assert is_ok(s), s
    return r["purchase_order_id"]


def _po_item_ids(conn, po_id):
    t = Table("purchase_order_item")
    q = Q.from_(t).select(t.id, t.item_id).where(t.purchase_order_id == P())
    return {r["item_id"]: r["id"]
            for r in conn.execute(q.get_sql(), (po_id,)).fetchall()}


def _series_rows(conn, entity_type, company_id):
    rows = _where(conn, "naming_series", entity_type=entity_type,
                  company_id=company_id)
    return {row["prefix"]: row for row in rows}


def _assert_series_advanced_by_one(before, after):
    """The counter advanced by exactly one and nothing else changed.

    The seeder writes a bare ``RFQ-`` row while ``get_next_name`` consumes
    from a year-scoped ``RFQ-2026-`` row it inserts on first use, so the
    prefix is not pinned literally — the single advance is the exact
    assertion. Returns the consumed prefix.
    """
    added = [prefix for prefix in after if prefix not in before]
    moved = [prefix for prefix in before if after[prefix] != before[prefix]]
    assert len(added) + len(moved) == 1
    if added:
        prefix = added[0]
        assert after[prefix]["current_value"] == 1
    else:
        prefix = moved[0]
        assert after[prefix]["current_value"] == (
            before[prefix]["current_value"] + 1)
    return prefix


# ---------------------------------------------------------------------------
# submit-rfq — stored row (no ledger: an RFQ submit names the document and
# stamps the supplier rows; it posts no GL, payment-ledger or stock rows).
# ---------------------------------------------------------------------------

class TestSubmitRfqDepth:
    def test_submit_names_the_document_and_stamps_every_supplier(self, conn, env):
        beta = seed_supplier(conn, env["company_id"], "Beta Metals")
        rfq_id = _add_rfq(conn, env, suppliers=[env["supplier"], beta])
        before = _row(conn, "request_for_quotation", rfq_id)
        assert (before["status"], before["naming_series"]) == ("draft", None)
        series_before = _series_rows(conn, "request_for_quotation",
                                     env["company_id"])
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id))
        assert is_ok(r), r
        assert r["rfq_id"] == rfq_id

        after = _row(conn, "request_for_quotation", rfq_id)
        assert after["status"] == "submitted"
        assert after["naming_series"] == r["naming_series"]
        assert after["naming_series"].startswith("RFQ-")
        assert after["company_id"] == env["company_id"]
        series_after = _series_rows(conn, "request_for_quotation",
                                      env["company_id"])
        used_prefix = _assert_series_advanced_by_one(series_before,
                                                     series_after)
        assert r["naming_series"].startswith(used_prefix)

        sent = _where(conn, "rfq_supplier", rfq_id=rfq_id)
        assert {s["supplier_id"] for s in sent} == {env["supplier"], beta}
        for row in sent:
            assert row["sent_date"] is not None
            assert (row["response_date"],
                    row["supplier_quotation_id"]) == (None, None)

        audits = _audit(conn, "submit-rfq", rfq_id)
        assert len(audits) == 1
        assert json.loads(audits[0]["new_values"]) == {
            "naming_series": r["naming_series"]}

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_submit_refusals_leave_the_database_identical(self, conn, env):
        rfq_id = _add_rfq(conn, env)
        assert is_ok(call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id)))
        snapshot = _snapshot(conn, _RFQ_TABLES + _LEDGERS)

        r = call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id))
        assert is_error(r)
        assert _msg(r) == "Cannot submit: RFQ is 'submitted' (must be 'draft')"
        r = call_action(B.submit_rfq, conn, ns(rfq_id="no-such-rfq"))
        assert is_error(r)
        assert _msg(r) == "RFQ no-such-rfq not found"
        r = call_action(B.submit_rfq, conn, ns(rfq_id=None))
        assert is_error(r)
        assert _msg(r) == "--rfq-id is required"

        assert _snapshot(conn, _RFQ_TABLES + _LEDGERS) == snapshot


# ---------------------------------------------------------------------------
# add-supplier-quotation — stored rows (no ledger: a quotation prices lines
# and links the supplier row; it posts no GL, payment-ledger or stock rows).
# ---------------------------------------------------------------------------

class TestAddSupplierQuotationDepth:
    def test_quotation_prices_lines_links_supplier_and_flips_rfq(self, conn, env):
        beta = seed_supplier(conn, env["company_id"], "Beta Metals")
        rfq_id = _add_rfq(conn, env, suppliers=[env["supplier"], beta])
        assert is_ok(call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id)))
        lines = _rfq_lines(conn, rfq_id)
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(B.add_supplier_quotation, conn, ns(
            rfq_id=rfq_id, supplier_id=env["supplier"],
            items=json.dumps([
                {"rfq_item_id": lines[env["item1"]], "rate": "1000.00",
                 "lead_time_days": 14},
                {"rfq_item_id": lines[env["item2"]], "rate": "12.50"}])))
        assert is_ok(r), r
        # 10 x 1000.00 + 4 x 12.50, computed by hand.
        assert r["total_amount"] == "10050.00"
        sq_id = r["supplier_quotation_id"]

        header = _row(conn, "supplier_quotation", sq_id)
        assert (header["supplier_id"], header["rfq_id"], header["company_id"],
                header["status"]) == (env["supplier"], rfq_id,
                                      env["company_id"], "draft")
        assert (header["total_amount"], header["grand_total"]) == (
            "10050.00", "10050.00")

        t = Table("supplier_quotation_item")
        q = (Q.from_(t)
             .select(t.item_id, t.quantity, t.rate, t.amount,
                     t.lead_time_days)
             .where(t.supplier_quotation_id == P()))
        stored_lines = sorted(
            tuple(row[key] for key in ("item_id", "quantity", "rate",
                                       "amount", "lead_time_days"))
            for row in conn.execute(q.get_sql(), (sq_id,)).fetchall())
        assert stored_lines == sorted([
            (env["item1"], "10.00", "1000.00", "10000.00", 14),
            (env["item2"], "4.00", "12.50", "50.00", None)])

        link = _where(conn, "rfq_supplier", rfq_id=rfq_id,
                      supplier_id=env["supplier"])
        assert len(link) == 1
        assert link[0]["supplier_quotation_id"] == sq_id
        assert link[0]["response_date"] is not None
        other = _where(conn, "rfq_supplier", rfq_id=rfq_id,
                       supplier_id=beta)
        assert (other[0]["response_date"],
                other[0]["supplier_quotation_id"]) == (None, None)
        assert _row(conn, "request_for_quotation",
                    rfq_id)["status"] == "submitted"

        beta_id = _quote(conn, rfq_id, beta, lines[env["item1"]], "900.00",
                         lines[env["item2"]], "10.25")
        assert _row(conn, "supplier_quotation",
                    beta_id)["total_amount"] == "9041.00"
        assert _row(conn, "request_for_quotation",
                    rfq_id)["status"] == "quotation_received"

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_quotation_refusals_leave_the_database_identical(self, conn, env):
        rfq_id = _add_rfq(conn, env)
        lines = _rfq_lines(conn, rfq_id)
        items = json.dumps([{"rfq_item_id": lines[env["item1"]],
                             "rate": "900.00"}])
        snapshot = _snapshot(conn, _RFQ_TABLES + _LEDGERS)

        r = call_action(B.add_supplier_quotation, conn, ns(
            rfq_id="no-such-rfq", supplier_id=env["supplier"], items=items))
        assert is_error(r)
        assert _msg(r) == "RFQ no-such-rfq not found"
        r = call_action(B.add_supplier_quotation, conn, ns(
            rfq_id=rfq_id, supplier_id="Nobody Ltd", items=items))
        assert is_error(r)
        assert _msg(r) == "Supplier Nobody Ltd not found"
        r = call_action(B.add_supplier_quotation, conn, ns(
            rfq_id=rfq_id, supplier_id=env["supplier"], items="[]"))
        assert is_error(r)
        assert _msg(r) == "--items must be a non-empty JSON array"
        r = call_action(B.add_supplier_quotation, conn, ns(
            rfq_id=None, supplier_id=env["supplier"], items=items))
        assert is_error(r)
        assert _msg(r) == "--rfq-id is required"
        r = call_action(B.add_supplier_quotation, conn, ns(
            rfq_id=rfq_id, supplier_id=None, items=items))
        assert is_error(r)
        assert _msg(r) == "--supplier-id is required"

        assert _snapshot(conn, _RFQ_TABLES + _LEDGERS) == snapshot


# ---------------------------------------------------------------------------
# compare-supplier-quotations — read-only. The comparison is pinned against
# the stored rates and every table is proved unchanged; no ledger assertion
# can hold for a pure read, so none is made.
# ---------------------------------------------------------------------------

class TestCompareSupplierQuotationsDepth:
    def test_compare_reports_stored_rates_and_writes_nothing(self, conn, env):
        beta = seed_supplier(conn, env["company_id"], "Beta Metals")
        rfq_id = _add_rfq(conn, env, suppliers=[env["supplier"], beta])
        assert is_ok(call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id)))
        lines = _rfq_lines(conn, rfq_id)
        _quote(conn, rfq_id, env["supplier"], lines[env["item1"]], "1000.00",
               lines[env["item2"]], "12.50")
        _quote(conn, rfq_id, beta, lines[env["item1"]], "900.00",
               lines[env["item2"]], "10.25")
        snapshot = _snapshot(conn, _RFQ_TABLES + _LEDGERS)

        r = call_action(B.compare_supplier_quotations, conn, ns(rfq_id=rfq_id))
        assert is_ok(r), r
        assert (r["rfq_id"], r["supplier_count"]) == (rfq_id, 2)
        assert len(r["comparison"]) == 2

        by_item = {entry["item_id"]: entry for entry in r["comparison"]}
        assert set(by_item) == {env["item1"], env["item2"]}
        assert (by_item[env["item1"]]["lowest_rate"],
                by_item[env["item1"]]["lowest_supplier"]) == ("900.00",
                                                              "Beta Metals")
        assert (by_item[env["item2"]]["lowest_rate"],
                by_item[env["item2"]]["lowest_supplier"]) == ("10.25",
                                                              "Beta Metals")
        assert sorted((q["supplier_name"], q["rate"], q["amount"],
                       q["is_lowest"])
                      for q in by_item[env["item1"]]["quotes"]) == [
            ("Acme Supplies", "1000.00", "10000.00", False),
            ("Beta Metals", "900.00", "9000.00", True)]

        t = Table("supplier_quotation_item").as_("sqi")
        sq = Table("supplier_quotation").as_("sq")
        q = (Q.from_(t).join(sq).on(sq.id == t.supplier_quotation_id)
             .select(sq.supplier_id, t.item_id, t.rate)
             .where(sq.rfq_id == P()))
        stored = {(row["supplier_id"], row["item_id"]): row["rate"]
                  for row in conn.execute(q.get_sql(), (rfq_id,)).fetchall()}
        reported = {}
        for entry in r["comparison"]:
            for quote in entry["quotes"]:
                reported[(quote["supplier_id"],
                          entry["item_id"])] = quote["rate"]
        assert reported == stored

        assert _snapshot(conn, _RFQ_TABLES + _LEDGERS) == snapshot

    def test_compare_refusals_leave_the_database_identical(self, conn, env):
        rfq_id = _add_rfq(conn, env)
        snapshot = _snapshot(conn, _RFQ_TABLES + _LEDGERS)

        r = call_action(B.compare_supplier_quotations, conn, ns(rfq_id=rfq_id))
        assert is_ok(r), r
        assert r["supplier_count"] == 0
        assert sorted((c["item_id"], c["quotes"], c["lowest_rate"],
                       c["lowest_supplier"]) for c in r["comparison"]) == sorted([
            (env["item1"], [], None, None),
            (env["item2"], [], None, None)])

        r = call_action(B.compare_supplier_quotations,
                        conn, ns(rfq_id="no-such-rfq"))
        assert is_error(r)
        assert _msg(r) == "RFQ no-such-rfq not found"
        r = call_action(B.compare_supplier_quotations, conn, ns(rfq_id=None))
        assert is_error(r)
        assert _msg(r) == "--rfq-id is required"

        assert _snapshot(conn, _RFQ_TABLES + _LEDGERS) == snapshot


# ---------------------------------------------------------------------------
# update-three-way-match-policy — stored row (no ledger: a company settings
# write; it posts no GL, payment-ledger or stock rows).
# ---------------------------------------------------------------------------

class TestUpdateThreeWayMatchPolicyDepth:
    def test_policy_moves_strict_to_tolerant_with_audit(self, conn, env):
        cid = env["company_id"]
        company = _row(conn, "company", cid)
        assert (company["three_way_match_policy"],
                company["receipt_tolerance_pct"]) == ("strict", "0")
        mine_before = len(_audit(conn, "update-three-way-match-policy", cid))
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(B.update_three_way_match_policy, conn, ns(
            company_id=cid, policy="tolerant"))
        assert is_ok(r), r
        assert (r["company_id"], r["three_way_match_policy"]) == (
            cid, "tolerant")

        after = _row(conn, "company", cid)
        assert after["three_way_match_policy"] == "tolerant"
        assert after["receipt_tolerance_pct"] == "0"
        for column, value in company.items():
            if column not in ("three_way_match_policy", "updated_at"):
                assert after[column] == value, column

        audits = _audit(conn, "update-three-way-match-policy", cid)
        assert len(audits) == mine_before + 1
        latest = audits[-1]
        assert latest["entity_type"] == "company"
        assert json.loads(latest["new_values"]) == {
            "three_way_match_policy": "tolerant"}

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_policy_refusals_leave_the_database_identical(self, conn, env):
        cid = env["company_id"]
        snapshot = _snapshot(conn, ("company", "audit_log") + _LEDGERS)

        r = call_action(B.update_three_way_match_policy, conn, ns(
            company_id=cid, policy="lenient"))
        assert is_error(r)
        assert _msg(r) == ("Invalid policy 'lenient'. Must be 'strict', "
                           "'tolerant', or 'disabled'.")
        r = call_action(B.update_three_way_match_policy, conn, ns(
            company_id=cid, policy=None))
        assert is_error(r)
        assert _msg(r) == ("Invalid policy 'None'. Must be 'strict', "
                           "'tolerant', or 'disabled'.")
        r = call_action(B.update_three_way_match_policy, conn, ns(
            company_id="no-such-company", policy="disabled"))
        assert is_error(r)
        assert _msg(r) == "Company no-such-company not found"
        r = call_action(B.update_three_way_match_policy, conn, ns(
            company_id=None, policy="disabled"))
        assert is_error(r)
        assert _msg(r) == "--company-id is required"

        assert _snapshot(conn, ("company", "audit_log") + _LEDGERS) == snapshot
        assert _row(conn, "company",
                    cid)["three_way_match_policy"] == "strict"


# ---------------------------------------------------------------------------
# get-purchase-order — read-only. The response is pinned field-by-field
# against the stored header and lines and every table is proved unchanged;
# no ledger assertion can hold for a pure read, so none is made.
# ---------------------------------------------------------------------------

class TestGetPurchaseOrderDepth:
    def test_get_returns_stored_header_lines_and_links(self, conn, env):
        po_id = _confirmed_po(conn, env)
        pois = _po_item_ids(conn, po_id)
        pr = call_action(B.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date=RECEIPT_DATE,
            items=json.dumps([{"purchase_order_item_id": pois[env["item1"]],
                               "qty": "4"}]),
            purchase_receipt_id=None))
        assert is_ok(pr), pr
        assert is_ok(call_action(B.submit_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"])))
        snapshot = _snapshot(conn, _DOC_TABLES)

        r = call_action(B.get_purchase_order, conn, ns(
            purchase_order_id=po_id))
        assert is_ok(r), r
        row = _row(conn, "purchase_order", po_id)
        # 10 x 50.00 + 5 x 20.00, no tax, computed by hand.
        assert (r["total_amount"], r["tax_amount"], r["grand_total"]) == (
            "600.00", "0.00", "600.00")
        assert (row["total_amount"], row["tax_amount"], row["grand_total"]) == (
            "600.00", "0.00", "600.00")
        assert r["document_status"] == row["status"] == "partially_received"
        assert r["order_date"] == row["order_date"] == PO_DATE

        assert sorted((i["item_name"], i["quantity"], i["rate"], i["amount"])
                      for i in r["items"]) == [
            ("Raw Material A", "10.00", "50.00", "500.00"),
            ("Raw Material B", "5.00", "20.00", "100.00")]
        stored = {i["id"]: i for i in _where(
            conn, "purchase_order_item", purchase_order_id=po_id)}
        assert set(stored) == {i["id"] for i in r["items"]}
        for line in r["items"]:
            for column, value in stored[line["id"]].items():
                assert line[column] == value, column
        assert Decimal(stored[pois[env["item1"]]]["received_qty"]) == Decimal("4")
        assert Decimal(stored[pois[env["item2"]]]["received_qty"]) == Decimal("0")

        assert [(x["id"], x["status"]) for x in r["purchase_receipts"]] == [
            (pr["purchase_receipt_id"], "submitted")]
        assert r["purchase_invoices"] == []

        assert _snapshot(conn, _DOC_TABLES) == snapshot

    def test_get_refusals_leave_the_database_identical(self, conn, env):
        po_id = _confirmed_po(conn, env)
        snapshot = _snapshot(conn, _DOC_TABLES)

        r = call_action(B.get_purchase_order, conn,
                        ns(purchase_order_id="no-such-doc"))
        assert is_error(r)
        assert _msg(r) == "Purchase order no-such-doc not found"
        r = call_action(B.get_purchase_order, conn,
                        ns(purchase_order_id=None))
        assert is_error(r)
        assert _msg(r) == "--purchase-order-id is required"
        assert _row(conn, "purchase_order", po_id)["status"] == "confirmed"

        assert _snapshot(conn, _DOC_TABLES) == snapshot


# ---------------------------------------------------------------------------
# get-purchase-receipt — read-only (same no-ledger note as get-purchase-order).
# ---------------------------------------------------------------------------

class TestGetPurchaseReceiptDepth:
    def test_get_returns_stored_lines_and_matches_stock(self, conn, env):
        po_id = _confirmed_po(conn, env)
        pois = _po_item_ids(conn, po_id)
        pr = call_action(B.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date=RECEIPT_DATE,
            items=json.dumps([{"purchase_order_item_id": pois[env["item1"]],
                               "qty": "4"}]),
            purchase_receipt_id=None))
        assert is_ok(pr), pr
        pr_id = pr["purchase_receipt_id"]
        assert is_ok(call_action(B.submit_purchase_receipt, conn, ns(
            purchase_receipt_id=pr_id)))
        snapshot = _snapshot(conn, _DOC_TABLES)

        r = call_action(B.get_purchase_receipt, conn, ns(
            purchase_receipt_id=pr_id))
        assert is_ok(r), r
        row = _row(conn, "purchase_receipt", pr_id)
        assert (r["document_status"], r["total_qty"], r["posting_date"],
                r["purchase_order_id"]) == ("submitted", "4.00",
                                            RECEIPT_DATE, po_id)
        assert row["total_qty"] == "4.00"
        assert [(i["item_name"], i["quantity"], i["rate"], i["amount"],
                 i["purchase_order_item_id"]) for i in r["items"]] == [
            ("Raw Material A", "4.00", "50.00", "200.00",
             pois[env["item1"]])]
        stored = {i["id"]: i for i in _where(
            conn, "purchase_receipt_item", purchase_receipt_id=pr_id)}
        for line in r["items"]:
            for column, value in stored[line["id"]].items():
                assert line[column] == value, column

        assert _snapshot(conn, _DOC_TABLES) == snapshot

    def test_get_refusals_leave_the_database_identical(self, conn, env):
        snapshot = _snapshot(conn, _DOC_TABLES)

        r = call_action(B.get_purchase_receipt, conn,
                        ns(purchase_receipt_id="no-such-doc"))
        assert is_error(r)
        assert _msg(r) == "Purchase receipt no-such-doc not found"
        r = call_action(B.get_purchase_receipt, conn,
                        ns(purchase_receipt_id=None))
        assert is_error(r)
        assert _msg(r) == "--purchase-receipt-id is required"

        assert _snapshot(conn, _DOC_TABLES) == snapshot


# ---------------------------------------------------------------------------
# get-purchase-invoice — read-only (same no-ledger note as get-purchase-order).
# ---------------------------------------------------------------------------

class TestGetPurchaseInvoiceDepth:
    def _standalone(self, conn, env):
        r = call_action(B.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date=INVOICE_DATE, due_date="2026-07-25",
            items=json.dumps([{"item_id": env["item1"], "qty": "3",
                               "rate": "100.00"}]),
            tax_template_id=None))
        assert is_ok(r), r
        assert (r["total_amount"], r["tax_amount"], r["grand_total"]) == (
            "300.00", "0.00", "300.00")
        return r["purchase_invoice_id"]

    def test_get_returns_stored_totals_lines_and_payments(self, conn, env):
        pi_id = self._standalone(conn, env)
        snapshot = _snapshot(conn, _DOC_TABLES)

        r = call_action(B.get_purchase_invoice, conn, ns(
            purchase_invoice_id=pi_id))
        assert is_ok(r), r
        row = _row(conn, "purchase_invoice", pi_id)
        assert (r["document_status"], r["total_amount"], r["tax_amount"],
                r["grand_total"], r["outstanding_amount"]) == (
            "draft", "300.00", "0.00", "300.00", "300.00")
        assert (row["total_amount"], row["tax_amount"], row["grand_total"],
                row["outstanding_amount"]) == (
            "300.00", "0.00", "300.00", "300.00")
        assert [(i["item_name"], i["quantity"], i["rate"], i["amount"])
                for i in r["items"]] == [
            ("Raw Material A", "3.00", "100.00", "300.00")]
        assert r["payments"] == []
        assert _snapshot(conn, _DOC_TABLES) == snapshot

        assert is_ok(call_action(B.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi_id)))
        snapshot = _snapshot(conn, _DOC_TABLES)
        r = call_action(B.get_purchase_invoice, conn, ns(
            purchase_invoice_id=pi_id))
        assert is_ok(r), r
        assert r["document_status"] == "submitted"
        assert (r["total_amount"], r["grand_total"],
                r["outstanding_amount"]) == ("300.00", "300.00", "300.00")
        stored_payments = _where(conn, "payment_ledger_entry",
                                 against_voucher_id=pi_id)
        assert len(stored_payments) == 1
        assert r["payments"] == stored_payments
        assert (stored_payments[0]["amount"],
                stored_payments[0]["account_id"],
                stored_payments[0]["party_id"]) == (
            "300.00", env["ap"], env["supplier"])
        assert _snapshot(conn, _DOC_TABLES) == snapshot

    def test_get_refusals_leave_the_database_identical(self, conn, env):
        pi_id = self._standalone(conn, env)
        snapshot = _snapshot(conn, _DOC_TABLES)

        r = call_action(B.get_purchase_invoice, conn,
                        ns(purchase_invoice_id="no-such-doc"))
        assert is_error(r)
        assert _msg(r) == "Purchase invoice no-such-doc not found"
        r = call_action(B.get_purchase_invoice, conn,
                        ns(purchase_invoice_id=None))
        assert is_error(r)
        assert _msg(r) == "--purchase-invoice-id is required"
        assert _row(conn, "purchase_invoice", pi_id)["status"] == "draft"

        assert _snapshot(conn, _DOC_TABLES) == snapshot


# ---------------------------------------------------------------------------
# create-debit-note — stored row. The note is born a draft, so it posts no
# GL, payment-ledger or stock rows; the test asserts that absence (a draft
# that posted would be the defect). The original bill is proved untouched.
# ---------------------------------------------------------------------------

class TestCreateDebitNoteDepth:
    def _submitted_bill(self, conn, env):
        r = call_action(B.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date=INVOICE_DATE, due_date="2026-07-25",
            items=json.dumps([
                {"item_id": env["item1"], "qty": "10", "rate": "100.00"},
                {"item_id": env["item2"], "qty": "4", "rate": "62.50"}]),
            tax_template_id=None))
        assert is_ok(r), r
        assert r["grand_total"] == "1250.00"
        pi_id = r["purchase_invoice_id"]
        assert is_ok(call_action(B.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi_id)))
        return pi_id

    def test_note_is_a_negative_draft_and_bill_is_untouched(self, conn, env):
        pi_id = self._submitted_bill(conn, env)
        orig_before = _row(conn, "purchase_invoice", pi_id)
        counts_before = {t: _count(conn, t) for t in _LEDGERS}

        r = call_action(B.create_debit_note, conn, ns(
            against_invoice_id=pi_id, posting_date="2026-06-28",
            reason="Defective goods",
            items=json.dumps([
                {"item_id": env["item1"], "qty": "3"},
                {"item_id": env["item2"], "qty": "2",
                 "rate": "62.50"}])))
        assert is_ok(r), r
        # 3 @ looked-up 100.00 + 2 x 62.50, negated, computed by hand.
        assert r["total_amount"] == "-425.00"
        assert r["against_invoice_id"] == pi_id
        dn_id = r["debit_note_id"]

        note = _row(conn, "purchase_invoice", dn_id)
        assert note["status"] == "draft"
        assert note["is_return"] == 1
        assert note["return_against"] == pi_id
        assert (note["supplier_id"], note["company_id"]) == (
            env["supplier"], env["company_id"])
        assert note["posting_date"] == "2026-06-28"
        assert (note["total_amount"], note["tax_amount"],
                note["grand_total"], note["outstanding_amount"]) == (
            "-425.00", "0", "-425.00", "-425.00")

        assert sorted(
            (row["item_id"], row["quantity"], row["rate"], row["amount"])
            for row in _where(conn, "purchase_invoice_item",
                              purchase_invoice_id=dn_id)) == sorted([
            (env["item1"], "-3.00", "100.00", "-300.00"),
            (env["item2"], "-2.00", "62.50", "-125.00")])

        assert _row(conn, "purchase_invoice", pi_id) == orig_before
        assert {t: _count(conn, t) for t in _LEDGERS} == counts_before
        assert len(_audit(conn, "create-debit-note", dn_id)) == 1

    def test_note_refusals_leave_the_database_identical(self, conn, env):
        pi_id = self._submitted_bill(conn, env)
        draft = call_action(B.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date=INVOICE_DATE, due_date="2026-07-25",
            items=json.dumps([{"item_id": env["item1"], "qty": "1",
                               "rate": "100.00"}]),
            tax_template_id=None))
        items = json.dumps([{"item_id": env["item1"], "qty": "1",
                             "rate": "100.00"}])
        snapshot = _snapshot(conn, ("purchase_invoice",
                                    "purchase_invoice_item",
                                    "audit_log") + _LEDGERS)

        r = call_action(B.create_debit_note, conn, ns(
            against_invoice_id=draft["purchase_invoice_id"],
            posting_date="2026-06-28", reason="x", items=items))
        assert is_error(r)
        assert _msg(r) == ("Cannot create debit note: invoice status is "
                           "'draft'")
        r = call_action(B.create_debit_note, conn, ns(
            against_invoice_id="no-such-bill", posting_date="2026-06-28",
            reason="x", items=items))
        assert is_error(r)
        assert _msg(r) == "Purchase invoice no-such-bill not found"
        r = call_action(B.create_debit_note, conn, ns(
            against_invoice_id=None, posting_date="2026-06-28",
            reason="x", items=items))
        assert is_error(r)
        assert _msg(r) == "--against-invoice-id is required"
        r = call_action(B.create_debit_note, conn, ns(
            against_invoice_id=pi_id, posting_date="2026-06-28",
            reason="x", items=None))
        assert is_error(r)
        assert _msg(r) == "--items is required (JSON array)"

        assert _snapshot(conn, ("purchase_invoice",
                                "purchase_invoice_item",
                                "audit_log") + _LEDGERS) == snapshot


# ---------------------------------------------------------------------------
# update-purchase-invoice — stored row. Only drafts can be updated, and a
# draft carries no GL, payment-ledger or stock rows; the test asserts the
# recomputed totals, the replaced lines, and that no ledger rows appeared.
# ---------------------------------------------------------------------------

class TestUpdatePurchaseInvoiceDepth:
    def _tax_template(self, conn, env):
        tax_account = _u()
        _insert(conn, "account", {
            "id": tax_account, "name": "Input Tax M478",
            "account_number": f"1400-{tax_account[:6]}", "root_type": "asset",
            "account_type": "tax", "balance_direction": "debit_normal",
            "company_id": env["company_id"], "depth": 0})
        tpl = _u()
        _insert(conn, "tax_template", {
            "id": tpl, "name": f"Purchase Tax 7.5 M478-{tpl[:4]}",
            "tax_type": "purchase", "company_id": env["company_id"]})
        _insert(conn, "tax_template_line", {
            "id": _u(), "tax_template_id": tpl, "tax_account_id": tax_account,
            "rate": "7.5", "charge_type": "on_net_total", "row_order": 0,
            "add_deduct": "add"})
        return tpl

    def _draft_bill(self, conn, env, tax_template_id=None):
        r = call_action(B.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date=INVOICE_DATE, due_date="2026-07-25",
            items=json.dumps([{"item_id": env["item1"], "qty": "5",
                               "rate": "100.00"}]),
            tax_template_id=tax_template_id))
        assert is_ok(r), r
        return r["purchase_invoice_id"]

    def test_update_recomputes_totals_and_replaces_lines(self, conn, env):
        tpl = self._tax_template(conn, env)
        pi_id = self._draft_bill(conn, env, tax_template_id=tpl)
        created = _row(conn, "purchase_invoice", pi_id)
        # 5 x 100.00 with 7.5 % tax, computed by hand.
        assert (created["total_amount"], created["tax_amount"],
                created["grand_total"],
                created["outstanding_amount"]) == (
            "500.00", "37.50", "537.50", "537.50")
        old_item_ids = {row["id"] for row in _where(
            conn, "purchase_invoice_item", purchase_invoice_id=pi_id)}
        assert len(old_item_ids) == 1

        r = call_action(B.update_purchase_invoice, conn, ns(
            purchase_invoice_id=pi_id, due_date="2026-08-15",
            items=json.dumps([
                {"item_id": env["item1"], "qty": "5", "rate": "45.50"},
                {"item_id": env["item2"], "qty": "2", "rate": "21.33"}])))
        assert is_ok(r), r
        assert r["updated_fields"] == ["due_date", "items"]

        header = _row(conn, "purchase_invoice", pi_id)
        assert header["status"] == "draft"
        assert header["due_date"] == "2026-08-15"
        # 227.50 + 42.66 = 270.16; 7.5 % = 20.262 -> 20.26, computed by hand.
        assert (header["total_amount"], header["tax_amount"],
                header["grand_total"],
                header["outstanding_amount"]) == (
            "270.16", "20.26", "290.42", "290.42")
        assert sorted(
            (row["item_id"], row["quantity"], row["rate"], row["amount"])
            for row in _where(conn, "purchase_invoice_item",
                              purchase_invoice_id=pi_id)) == sorted([
            (env["item1"], "5.00", "45.50", "227.50"),
            (env["item2"], "2.00", "21.33", "42.66")])
        new_item_ids = {row["id"] for row in _where(
            conn, "purchase_invoice_item", purchase_invoice_id=pi_id)}
        assert len(new_item_ids) == 2 and not (new_item_ids & old_item_ids)
        assert _gl(conn, "purchase_invoice", pi_id) == []
        assert _where(conn, "payment_ledger_entry",
                      against_voucher_id=pi_id) == []
        assert len(_audit(conn, "update-purchase-invoice", pi_id)) == 1

    def test_update_refusals_leave_the_database_identical(self, conn, env):
        pi_id = self._draft_bill(conn, env)
        assert is_ok(call_action(B.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi_id)))
        draft_id = self._draft_bill(conn, env)
        snapshot = _snapshot(conn, ("purchase_invoice",
                                    "purchase_invoice_item",
                                    "audit_log") + _LEDGERS)
        submitted_gl = _gl(conn, "purchase_invoice", pi_id)

        r = call_action(B.update_purchase_invoice, conn, ns(
            purchase_invoice_id=pi_id, due_date="2026-09-30",
            items=json.dumps([{"item_id": env["item1"], "qty": "9",
                               "rate": "100.00"}])))
        assert is_error(r)
        assert _msg(r) == ("Cannot update: invoice is 'submitted' "
                           "(must be 'draft')")
        r = call_action(B.update_purchase_invoice, conn, ns(
            purchase_invoice_id=draft_id, due_date=None, items=None))
        assert is_error(r)
        assert _msg(r) == "No fields to update"
        r = call_action(B.update_purchase_invoice, conn, ns(
            purchase_invoice_id="no-such-bill", due_date="2026-09-30",
            items=None))
        assert is_error(r)
        assert _msg(r) == "Purchase invoice no-such-bill not found"
        r = call_action(B.update_purchase_invoice, conn, ns(
            purchase_invoice_id=None, due_date="2026-09-30", items=None))
        assert is_error(r)
        assert _msg(r) == "--purchase-invoice-id is required"

        assert _snapshot(conn, ("purchase_invoice",
                                "purchase_invoice_item",
                                "audit_log") + _LEDGERS) == snapshot
        assert _gl(conn, "purchase_invoice", pi_id) == submitted_gl


# ---------------------------------------------------------------------------
# import-suppliers — DEFECT, documented not fixed (task rule 6).
#
# Finding M478-1: any CSV containing a supplier that does not already exist
# makes the action raise ``ValueError: Unknown entity type 'supplier'``
# (``erpclaw_lib.naming.ENTITY_PREFIXES`` has no ``supplier`` key), so no
# import has ever succeeded. The only path that returns ``ok`` is the
# all-duplicates path, which skips every row before reaching the naming
# call. Both behaviours are pinned below; the validation refusals after
# them prove the refusal layer itself writes nothing.
# ---------------------------------------------------------------------------

class TestImportSuppliersDepth:
    TABLES = ("supplier", "naming_series", "audit_log") + _LEDGERS

    def _csv(self, tmp_path, name, body):
        path = str(tmp_path / name)
        with open(path, "w", newline="") as handle:
            handle.write(body)
        return path

    def test_valid_import_creates_both_suppliers(self, conn, env, tmp_path):
        company = _row(conn, "company", env["company_id"])
        company_currency = company["default_currency"]
        assert company_currency == "USD"
        assert _count(conn, "supplier") == 1
        path = self._csv(
            tmp_path, "suppliers.csv",
            "name,supplier_type,country,email\n"
            "Acme Bulk,Company,USA,bulk@acme.example\n"
            "Beta Bulk,Company,USA,bulk@beta.example\n")

        r = call_action(B.import_suppliers, conn, ns(
            csv_path=path, company_id=env["company_id"]))
        assert is_ok(r), r
        assert (r["imported"], r["skipped"], r["total_rows"]) == (2, 0, 2)
        assert _count(conn, "supplier") == 3
        acme = _where(conn, "supplier", name="Acme Bulk",
                      company_id=env["company_id"])
        beta = _where(conn, "supplier", name="Beta Bulk",
                      company_id=env["company_id"])
        assert len(acme) == 1
        assert len(beta) == 1
        assert acme[0]["supplier_type"] == "company"
        assert beta[0]["supplier_type"] == "company"
        assert acme[0]["company_id"] == env["company_id"]
        assert beta[0]["company_id"] == env["company_id"]
        assert acme[0]["default_currency"] == company_currency
        assert beta[0]["default_currency"] == company_currency
        assert acme[0]["email"] == "bulk@acme.example"
        assert beta[0]["email"] == "bulk@beta.example"

    def test_all_duplicates_returns_ok_and_changes_nothing(self, conn, env,
                                                           tmp_path):
        for name in ("Acme Bulk", "Beta Bulk"):
            _insert(conn, "supplier", {
                "id": _u(), "name": name, "company_id": env["company_id"],
                "supplier_type": "company", "status": "active"})
        before = _snapshot(conn, self.TABLES)
        path = self._csv(
            tmp_path, "suppliers.csv",
            "name,supplier_type\nAcme Bulk,Company\nBeta Bulk,Company\n")

        r = call_action(B.import_suppliers, conn, ns(
            csv_path=path, company_id=env["company_id"]))
        assert is_ok(r), r
        assert (r["imported"], r["skipped"], r["total_rows"]) == (0, 2, 2)
        assert _snapshot(conn, self.TABLES) == before

    def test_import_refusals_leave_the_database_identical(self, conn, env,
                                                          tmp_path):
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.import_suppliers, conn, ns(
            csv_path=None, company_id=env["company_id"]))
        assert is_error(r)
        assert _msg(r) == "--csv-path is required"
        r = call_action(B.import_suppliers, conn, ns(
            csv_path=str(tmp_path / "suppliers.csv"), company_id=None))
        assert is_error(r)
        assert _msg(r) == "--company-id is required"

        not_csv = self._csv(tmp_path, "suppliers.txt", "name\nAcme Bulk\n")
        r = call_action(B.import_suppliers, conn, ns(
            csv_path=not_csv, company_id=env["company_id"]))
        assert is_error(r)
        assert _msg(r) == "--csv-path must point to a .csv file"

        missing = str(tmp_path / "absent.csv")
        r = call_action(B.import_suppliers, conn, ns(
            csv_path=missing, company_id=env["company_id"]))
        assert is_error(r)
        assert _msg(r) == f"File not found: {missing}"

        bad = self._csv(tmp_path, "bad.csv", "title\nAcme Bulk\n")
        r = call_action(B.import_suppliers, conn, ns(
            csv_path=bad, company_id=env["company_id"]))
        assert is_error(r)
        assert _msg(r) == ("CSV validation failed: "
                           "Missing required column: name")

        assert _snapshot(conn, self.TABLES) == snapshot
