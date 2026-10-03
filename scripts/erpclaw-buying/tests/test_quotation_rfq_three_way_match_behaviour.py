"""Behaviour of the request-for-quotation cycle, the three-way-match policy and
the purchase order / receipt / invoice read actions, read back from the database.

Actions: submit-rfq, add-supplier-quotation, compare-supplier-quotations,
update-three-way-match-policy, get-purchase-order, get-purchase-receipt and
get-purchase-invoice. Every document date is fixed. The RFQ and quotation dates
are stamped by the handlers with today's date and the naming series carry the
current year, so neither is pinned literally.

RFQ scenario: Raw Material A x 10 and Raw Material B x 4, sent to three suppliers.
  Acme Supplies  A 1000.00 (10000.00)  B 12.50 (50.00)  total 10050.00
  Beta Metals    A  900.00  (9000.00)  B 10.25 (41.00)  total  9041.00
  Gamma Parts    A  999.99  (9999.90)  B  9.95 (39.80)  total 10039.70
As text "1000.00" sorts below "900.00" and "10.25" below "9.95"; by number the
cheapest are Beta Metals for A and Gamma Parts for B.
"""
import json
import os
import sys
import uuid
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from buying_helpers import (call_action, is_error, is_ok, load_db_query,  # noqa: E402
                            ns, seed_supplier)

B = load_db_query()

PO_DATE = "2026-06-15"
RECEIPT_DATE = "2026-06-20"
INVOICE_DATE = "2026-06-25"


def _u():
    return str(uuid.uuid4())


def _msg(result):
    return result.get("message", "")


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _counts(conn, *tables):
    return tuple(_count(conn, t) for t in tables)


def _row(conn, table, row_id):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


def _audit_count(conn, action, entity_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE skill = 'erpclaw-buying' "
        "AND action = ? AND entity_id = ?", (action, entity_id)).fetchone()["n"]


def _gl(conn, voucher_type, voucher_id):
    return [dict(r) for r in conn.execute(
        "SELECT account_id, debit, credit, party_type, party_id, posting_date, "
        "voucher_type, voucher_id, is_cancelled FROM gl_entry "
        "WHERE voucher_type = ? AND voucher_id = ?", (voucher_type, voucher_id)).fetchall()]


def _legs(rows):
    return sorted((r["account_id"], r["debit"], r["credit"]) for r in rows)


def _totals(rows):
    debit = sum((Decimal(r["debit"]) for r in rows), Decimal("0"))
    credit = sum((Decimal(r["credit"]) for r in rows), Decimal("0"))
    return str(debit), str(credit)


# ---------------------------------------------------------------------------
# RFQ and quotation scenario
# ---------------------------------------------------------------------------

def _rfq(conn, env):
    env["beta"] = seed_supplier(conn, env["company_id"], "Beta Metals")
    env["gamma"] = seed_supplier(conn, env["company_id"], "Gamma Parts")
    r = call_action(B.add_rfq, conn, ns(
        company_id=env["company_id"],
        items=json.dumps([{"item_id": env["item1"], "qty": "10"},
                          {"item_id": env["item2"], "qty": "4"}]),
        suppliers=json.dumps([env["supplier"], env["beta"], env["gamma"]])))
    assert is_ok(r), r
    rfq_id = r["rfq_id"]
    lines = {row["item_id"]: row["id"] for row in conn.execute(
        "SELECT id, item_id FROM rfq_item WHERE rfq_id = ?", (rfq_id,)).fetchall()}
    return rfq_id, lines[env["item1"]], lines[env["item2"]]


def _quote(conn, rfq_id, supplier_id, line_a, rate_a, line_b, rate_b, lead_a=None):
    return call_action(B.add_supplier_quotation, conn, ns(
        rfq_id=rfq_id, supplier_id=supplier_id,
        items=json.dumps([{"rfq_item_id": line_a, "rate": rate_a, "lead_time_days": lead_a},
                          {"rfq_item_id": line_b, "rate": rate_b}])))


def _rfq_suppliers(conn, rfq_id):
    return {r["supplier_id"]: dict(r) for r in conn.execute(
        "SELECT supplier_id, sent_date, response_date, supplier_quotation_id "
        "FROM rfq_supplier WHERE rfq_id = ?", (rfq_id,)).fetchall()}


def _sq_lines(conn, sq_id):
    return sorted((r["item_id"], r["quantity"], r["rate"], r["amount"], r["lead_time_days"])
                  for r in conn.execute(
                      "SELECT item_id, quantity, rate, amount, lead_time_days "
                      "FROM supplier_quotation_item WHERE supplier_quotation_id = ?",
                      (sq_id,)).fetchall())


def test_submit_rfq_marks_it_submitted_and_sent_to_every_supplier(conn, env):
    rfq_id, _, _ = _rfq(conn, env)
    before = _row(conn, "request_for_quotation", rfq_id)
    assert (before["status"], before["naming_series"]) == ("draft", None)
    assert all(s["sent_date"] is None for s in _rfq_suppliers(conn, rfq_id).values())

    r = call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id))
    assert is_ok(r), r
    assert r["rfq_id"] == rfq_id
    assert r["document_status"] == "submitted"

    after = _row(conn, "request_for_quotation", rfq_id)
    assert after["status"] == "submitted"
    assert after["naming_series"] == r["naming_series"]
    assert after["company_id"] == env["company_id"]
    suppliers = _rfq_suppliers(conn, rfq_id)
    assert set(suppliers) == {env["supplier"], env["beta"], env["gamma"]}
    for s in suppliers.values():
        assert s["sent_date"] is not None
        assert (s["response_date"], s["supplier_quotation_id"]) == (None, None)
    assert _count(conn, "rfq_item") == 2
    assert _audit_count(conn, "submit-rfq", rfq_id) == 1


def test_submit_rfq_refuses_resubmit_unknown_and_missing_and_writes_nothing(conn, env):
    rfq_id, line_a, line_b = _rfq(conn, env)
    first = call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id))
    assert is_ok(first), first
    snapshot = _row(conn, "request_for_quotation", rfq_id)
    sent = _rfq_suppliers(conn, rfq_id)
    series = [dict(r) for r in conn.execute(
        "SELECT entity_type, prefix, current_value FROM naming_series "
        "WHERE company_id = ? ORDER BY entity_type, prefix", (env["company_id"],)).fetchall()]
    audits = _count(conn, "audit_log")

    r = call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id))
    assert is_error(r)
    assert _msg(r) == "Cannot submit: RFQ is 'submitted' (must be 'draft')"
    r = call_action(B.submit_rfq, conn, ns(rfq_id="no-such-rfq"))
    assert _msg(r) == "RFQ no-such-rfq not found"
    r = call_action(B.submit_rfq, conn, ns(rfq_id=None))
    assert _msg(r) == "--rfq-id is required"

    assert _row(conn, "request_for_quotation", rfq_id) == snapshot
    assert _rfq_suppliers(conn, rfq_id) == sent
    assert [dict(r) for r in conn.execute(
        "SELECT entity_type, prefix, current_value FROM naming_series "
        "WHERE company_id = ? ORDER BY entity_type, prefix",
        (env["company_id"],)).fetchall()] == series
    assert _count(conn, "audit_log") == audits

    for supplier, a, b in ((env["supplier"], "1000.00", "12.50"),
                           (env["beta"], "900.00", "10.25"),
                           (env["gamma"], "999.99", "9.95")):
        assert is_ok(_quote(conn, rfq_id, supplier, line_a, a, line_b, b))
    assert _row(conn, "request_for_quotation", rfq_id)["status"] == "quotation_received"
    r = call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id))
    assert _msg(r) == "Cannot submit: RFQ is 'quotation_received' (must be 'draft')"
    assert _row(conn, "request_for_quotation", rfq_id)["status"] == "quotation_received"

    # An RFQ with no suppliers or no items cannot be created, so it never reaches submit.
    counts = _counts(conn, "request_for_quotation", "rfq_item", "rfq_supplier")
    r = call_action(B.add_rfq, conn, ns(company_id=env["company_id"],
                                        items=json.dumps([{"item_id": env["item1"], "qty": "1"}]),
                                        suppliers="[]"))
    assert _msg(r) == "--suppliers must be a non-empty JSON array"
    r = call_action(B.add_rfq, conn, ns(company_id=env["company_id"], items="[]",
                                        suppliers=json.dumps([env["supplier"]])))
    assert _msg(r) == "--items must be a non-empty JSON array"
    assert _counts(conn, "request_for_quotation", "rfq_item", "rfq_supplier") == counts


def test_add_supplier_quotation_prices_lines_at_the_rfq_quantity(conn, env):
    rfq_id, line_a, line_b = _rfq(conn, env)
    assert is_ok(call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id)))

    r = _quote(conn, rfq_id, env["supplier"], line_a, "1000.00", line_b, "12.5", lead_a=14)
    assert is_ok(r), r
    assert r["total_amount"] == "10050.00"
    acme = r["supplier_quotation_id"]
    sq = _row(conn, "supplier_quotation", acme)
    assert (sq["supplier_id"], sq["rfq_id"], sq["company_id"], sq["status"]) == (
        env["supplier"], rfq_id, env["company_id"], "draft")
    assert (sq["total_amount"], sq["grand_total"]) == ("10050.00", "10050.00")
    assert _sq_lines(conn, acme) == sorted([
        (env["item1"], "10.00", "1000.00", "10000.00", 14),
        (env["item2"], "4.00", "12.50", "50.00", None)])
    suppliers = _rfq_suppliers(conn, rfq_id)
    assert suppliers[env["supplier"]]["supplier_quotation_id"] == acme
    assert suppliers[env["supplier"]]["response_date"] is not None
    for other in (env["beta"], env["gamma"]):
        assert (suppliers[other]["response_date"], suppliers[other]["supplier_quotation_id"]) == (None, None)
    assert _row(conn, "request_for_quotation", rfq_id)["status"] == "submitted"
    assert _audit_count(conn, "add-supplier-quotation", acme) == 1

    # The supplier may be named instead of identified; the row stores the id.
    r = _quote(conn, rfq_id, "Beta Metals", line_a, "900.00", line_b, "10.25")
    assert is_ok(r), r
    assert r["total_amount"] == "9041.00"
    beta = r["supplier_quotation_id"]
    assert _row(conn, "supplier_quotation", beta)["supplier_id"] == env["beta"]
    assert _sq_lines(conn, beta) == sorted([
        (env["item1"], "10.00", "900.00", "9000.00", None),
        (env["item2"], "4.00", "10.25", "41.00", None)])
    assert _row(conn, "request_for_quotation", rfq_id)["status"] == "submitted"

    r = _quote(conn, rfq_id, env["gamma"], line_a, "999.99", line_b, "9.95")
    assert is_ok(r), r
    assert r["total_amount"] == "10039.70"
    gamma = r["supplier_quotation_id"]
    assert (_row(conn, "supplier_quotation", gamma)["total_amount"],
            _row(conn, "supplier_quotation", gamma)["grand_total"]) == ("10039.70", "10039.70")
    assert _row(conn, "request_for_quotation", rfq_id)["status"] == "quotation_received"
    assert {s: v["supplier_quotation_id"] for s, v in _rfq_suppliers(conn, rfq_id).items()} == {
        env["supplier"]: acme, env["beta"]: beta, env["gamma"]: gamma}
    assert _count(conn, "supplier_quotation") == 3
    assert _count(conn, "supplier_quotation_item") == 6


def test_add_supplier_quotation_refusals_write_nothing(conn, env):
    rfq_id, line_a, line_b = _rfq(conn, env)
    assert is_ok(call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id)))
    counts = _counts(conn, "supplier_quotation", "supplier_quotation_item", "audit_log")
    rfq_row = _row(conn, "request_for_quotation", rfq_id)
    sent = _rfq_suppliers(conn, rfq_id)
    items = json.dumps([{"rfq_item_id": line_a, "rate": "900.00"}])

    r = call_action(B.add_supplier_quotation, conn, ns(
        rfq_id="no-such-rfq", supplier_id=env["supplier"], items=items))
    assert is_error(r)
    assert _msg(r) == "RFQ no-such-rfq not found"
    r = call_action(B.add_supplier_quotation, conn, ns(
        rfq_id=rfq_id, supplier_id="Nobody Ltd", items=items))
    assert _msg(r) == "Supplier Nobody Ltd not found"
    r = call_action(B.add_supplier_quotation, conn, ns(
        rfq_id=rfq_id, supplier_id=env["supplier"], items="[]"))
    assert _msg(r) == "--items must be a non-empty JSON array"
    r = call_action(B.add_supplier_quotation, conn, ns(
        rfq_id=None, supplier_id=env["supplier"], items=items))
    assert _msg(r) == "--rfq-id is required"
    r = call_action(B.add_supplier_quotation, conn, ns(
        rfq_id=rfq_id, supplier_id=None, items=items))
    assert _msg(r) == "--supplier-id is required"

    assert _counts(conn, "supplier_quotation", "supplier_quotation_item", "audit_log") == counts
    assert _row(conn, "request_for_quotation", rfq_id) == rfq_row
    assert _rfq_suppliers(conn, rfq_id) == sent


def test_compare_supplier_quotations_picks_the_numerically_lowest_rate_per_item(conn, env):
    rfq_id, line_a, line_b = _rfq(conn, env)
    assert is_ok(call_action(B.submit_rfq, conn, ns(rfq_id=rfq_id)))
    for supplier, a, b, lead in ((env["supplier"], "1000.00", "12.50", 5),
                                 (env["beta"], "900.00", "10.25", 21),
                                 (env["gamma"], "999.99", "9.95", 9)):
        assert is_ok(_quote(conn, rfq_id, supplier, line_a, a, line_b, b, lead_a=lead))
    counts = _counts(conn, "supplier_quotation", "supplier_quotation_item",
                     "rfq_supplier", "audit_log")

    r = call_action(B.compare_supplier_quotations, conn, ns(rfq_id=rfq_id))
    assert is_ok(r), r
    assert r["rfq_id"] == rfq_id
    assert r["supplier_count"] == 3
    by_item = {c["item_id"]: c for c in r["comparison"]}
    assert set(by_item) == {env["item1"], env["item2"]}
    assert len(r["comparison"]) == 2

    a = by_item[env["item1"]]
    assert (a["item_name"], a["required_qty"]) == ("Raw Material A", "10.00")
    assert (a["lowest_rate"], a["lowest_supplier"]) == ("900.00", "Beta Metals")
    assert sorted((q["supplier_name"], q["supplier_id"], q["rate"], q["amount"],
                   q["lead_time_days"], q["is_lowest"]) for q in a["quotes"]) == [
        ("Acme Supplies", env["supplier"], "1000.00", "10000.00", 5, False),
        ("Beta Metals", env["beta"], "900.00", "9000.00", 21, True),
        ("Gamma Parts", env["gamma"], "999.99", "9999.90", 9, False)]

    b = by_item[env["item2"]]
    assert (b["item_name"], b["required_qty"]) == ("Raw Material B", "4.00")
    assert (b["lowest_rate"], b["lowest_supplier"]) == ("9.95", "Gamma Parts")
    assert sorted((q["supplier_name"], q["rate"], q["amount"], q["is_lowest"])
                  for q in b["quotes"]) == [
        ("Acme Supplies", "12.50", "50.00", False),
        ("Beta Metals", "10.25", "41.00", False),
        ("Gamma Parts", "9.95", "39.80", True)]

    # The rates reported are the stored rates.
    stored = {(row["supplier_id"], row["item_id"]): row["rate"] for row in conn.execute(
        "SELECT sq.supplier_id, sqi.item_id, sqi.rate FROM supplier_quotation_item sqi "
        "JOIN supplier_quotation sq ON sq.id = sqi.supplier_quotation_id "
        "WHERE sq.rfq_id = ?", (rfq_id,)).fetchall()}
    assert {(q["supplier_id"], env["item1"]): q["rate"] for q in a["quotes"]} == {
        k: v for k, v in stored.items() if k[1] == env["item1"]}
    assert _counts(conn, "supplier_quotation", "supplier_quotation_item",
                   "rfq_supplier", "audit_log") == counts


def test_compare_supplier_quotations_without_quotes_and_refusals(conn, env):
    rfq_id, _, _ = _rfq(conn, env)
    counts = _counts(conn, "request_for_quotation", "supplier_quotation", "audit_log")

    r = call_action(B.compare_supplier_quotations, conn, ns(rfq_id=rfq_id))
    assert is_ok(r), r
    assert r["supplier_count"] == 0
    assert sorted((c["item_id"], c["required_qty"], c["quotes"], c["lowest_rate"],
                   c["lowest_supplier"]) for c in r["comparison"]) == sorted([
        (env["item1"], "10.00", [], None, None),
        (env["item2"], "4.00", [], None, None)])

    r = call_action(B.compare_supplier_quotations, conn, ns(rfq_id="no-such-rfq"))
    assert is_error(r)
    assert _msg(r) == "RFQ no-such-rfq not found"
    r = call_action(B.compare_supplier_quotations, conn, ns(rfq_id=None))
    assert _msg(r) == "--rfq-id is required"
    assert _counts(conn, "request_for_quotation", "supplier_quotation", "audit_log") == counts


# ---------------------------------------------------------------------------
# Three-way match policy
# ---------------------------------------------------------------------------

def _confirmed_po(conn, env, lines, tax_template_id=None):
    r = call_action(B.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"], posting_date=PO_DATE,
        items=json.dumps([{"item_id": env[k], "qty": q, "rate": rate,
                           "warehouse_id": env["warehouse"]} for k, q, rate in lines]),
        tax_template_id=tax_template_id, name=None))
    assert is_ok(r), r
    s = call_action(B.submit_purchase_order, conn, ns(purchase_order_id=r["purchase_order_id"]))
    assert is_ok(s), s
    return r


def _receipt(conn, po_id, items=None, posting_date=RECEIPT_DATE):
    r = call_action(B.create_purchase_receipt, conn, ns(
        purchase_order_id=po_id, company_id=None, posting_date=posting_date,
        items=items, purchase_receipt_id=None))
    assert is_ok(r), r
    s = call_action(B.submit_purchase_receipt, conn, ns(purchase_receipt_id=r["purchase_receipt_id"]))
    assert is_ok(s), s
    return r["purchase_receipt_id"]


def _invoice_from_po(conn, po_id):
    r = call_action(B.create_purchase_invoice, conn, ns(
        purchase_order_id=po_id, purchase_receipt_id=None, supplier_id=None,
        company_id=None, posting_date=INVOICE_DATE, due_date="2026-07-25",
        items=None, tax_template_id=None))
    assert is_ok(r), r
    return r


def _company_policy(conn, company_id):
    row = conn.execute("SELECT three_way_match_policy, receipt_tolerance_pct FROM company "
                       "WHERE id = ?", (company_id,)).fetchone()
    return row["three_way_match_policy"], row["receipt_tolerance_pct"]


def _set_tolerance(conn, company_id, pct):
    r = call_action(B.update_receipt_tolerance, conn, ns(company_id=company_id, tolerance_pct=pct))
    assert is_ok(r), r


def test_three_way_match_policy_blocks_beyond_tolerance_and_passes_within(conn, env):
    cid = env["company_id"]
    po = _confirmed_po(conn, env, [("item1", "10", "50.00")])
    po_id = po["purchase_order_id"]
    poi = conn.execute("SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
                       (po_id,)).fetchone()["id"]
    _receipt(conn, po_id, items=json.dumps([{"purchase_order_item_id": poi, "qty": "8"}]))
    pi = _invoice_from_po(conn, po_id)
    pi_id = pi["purchase_invoice_id"]
    assert pi["grand_total"] == "500.00"
    assert _company_policy(conn, cid) == ("strict", "0")

    blocked = ("Invoice qty exceeds received qty for item 'Raw Material A'. "
               "Ordered: 10.00, Received: 8.00, Already invoiced: 0, Current invoice: 10.00")

    def _refused():
        r = call_action(B.submit_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
        assert is_error(r), r
        assert _msg(r) == blocked
        row = _row(conn, "purchase_invoice", pi_id)
        assert (row["status"], row["naming_series"], row["outstanding_amount"]) == (
            "draft", None, "500.00")
        assert _gl(conn, "purchase_invoice", pi_id) == []
        assert _count(conn, "payment_ledger_entry") == 0
        assert _row(conn, "purchase_order_item", poi)["invoiced_qty"] == "0"
        assert _row(conn, "purchase_order", po_id)["status"] == "partially_received"

    # Strict, no tolerance: 10 invoiced against 8 received.
    _refused()
    # A tolerance alone does not relax a strict policy.
    _set_tolerance(conn, cid, "25")
    _refused()

    r = call_action(B.update_three_way_match_policy, conn, ns(company_id=cid, policy="tolerant"))
    assert is_ok(r), r
    assert (r["company_id"], r["three_way_match_policy"]) == (cid, "tolerant")
    assert _company_policy(conn, cid) == ("tolerant", "25.00")
    audit = conn.execute(
        "SELECT entity_type, new_values FROM audit_log WHERE skill = 'erpclaw-buying' "
        "AND action = 'update-three-way-match-policy' AND entity_id = ?", (cid,)).fetchall()
    assert [(a["entity_type"], json.loads(a["new_values"])) for a in audit] == [
        ("company", {"three_way_match_policy": "tolerant"})]

    # Tolerant at 20 %: 8 received allows 9.60, still short of 10.
    _set_tolerance(conn, cid, "20")
    assert _company_policy(conn, cid) == ("tolerant", "20.00")
    _refused()

    # Tolerant at 25 %: 8 received allows 10.00, the invoice passes.
    _set_tolerance(conn, cid, "25")
    r = call_action(B.submit_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
    assert is_ok(r), r
    row = _row(conn, "purchase_invoice", pi_id)
    assert (row["status"], row["grand_total"], row["outstanding_amount"]) == (
        "submitted", "500.00", "500.00")
    assert row["naming_series"] == r["naming_series"]
    gl = _gl(conn, "purchase_invoice", pi_id)
    assert _legs(gl) == sorted([(env["srnb"], "500.00", "0.00"), (env["ap"], "0.00", "500.00")])
    assert _totals(gl) == ("500.00", "500.00")
    assert all((g["posting_date"], g["is_cancelled"]) == (INVOICE_DATE, 0) for g in gl)
    assert [(g["party_type"], g["party_id"]) for g in gl if g["account_id"] == env["ap"]] == [
        ("supplier", env["supplier"])]
    ple = [dict(p) for p in conn.execute(
        "SELECT amount, against_voucher_id FROM payment_ledger_entry").fetchall()]
    assert ple == [{"amount": "500.00", "against_voucher_id": pi_id}]
    assert Decimal(_row(conn, "purchase_order_item", poi)["invoiced_qty"]) == Decimal("10")


def test_three_way_match_disabled_policy_skips_the_check(conn, env):
    cid = env["company_id"]
    po_id = _confirmed_po(conn, env, [("item1", "10", "50.00")])["purchase_order_id"]
    poi = conn.execute("SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
                       (po_id,)).fetchone()["id"]
    _receipt(conn, po_id, items=json.dumps([{"purchase_order_item_id": poi, "qty": "5"}]))
    pi_id = _invoice_from_po(conn, po_id)["purchase_invoice_id"]

    r = call_action(B.update_three_way_match_policy, conn, ns(company_id=cid, policy="disabled"))
    assert is_ok(r), r
    assert _company_policy(conn, cid) == ("disabled", "0")
    r = call_action(B.submit_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
    assert is_ok(r), r
    assert _totals(_gl(conn, "purchase_invoice", pi_id)) == ("500.00", "500.00")

    r = call_action(B.update_three_way_match_policy, conn, ns(company_id=cid, policy="strict"))
    assert is_ok(r), r
    assert _company_policy(conn, cid) == ("strict", "0")


def test_update_three_way_match_policy_refusals_write_nothing(conn, env):
    cid = env["company_id"]
    company = _row(conn, "company", cid)
    audits = _count(conn, "audit_log")

    r = call_action(B.update_three_way_match_policy, conn, ns(company_id=cid, policy="lenient"))
    assert is_error(r)
    assert _msg(r) == "Invalid policy 'lenient'. Must be 'strict', 'tolerant', or 'disabled'."
    r = call_action(B.update_three_way_match_policy, conn, ns(company_id=cid, policy=None))
    assert _msg(r) == "Invalid policy 'None'. Must be 'strict', 'tolerant', or 'disabled'."
    r = call_action(B.update_three_way_match_policy, conn, ns(
        company_id="no-such-company", policy="disabled"))
    assert _msg(r) == "Company no-such-company not found"
    r = call_action(B.update_three_way_match_policy, conn, ns(company_id=None, policy="disabled"))
    assert _msg(r) == "--company-id is required"

    assert _row(conn, "company", cid) == company
    assert _company_policy(conn, cid) == ("strict", "0")
    assert _count(conn, "audit_log") == audits


# ---------------------------------------------------------------------------
# get-purchase-order / get-purchase-receipt / get-purchase-invoice
# ---------------------------------------------------------------------------

def _tax_setup(conn, env):
    cid = env["company_id"]
    env["input_tax"] = _u()
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type, "
        "balance_direction, company_id, depth) VALUES (?, 'Input Tax', '1400', 'asset', "
        "'tax', 'debit_normal', ?, 0)", (env["input_tax"], cid))
    env["tax_tpl"] = _u()
    conn.execute("INSERT INTO tax_template (id, name, tax_type, company_id) "
                 "VALUES (?, 'Purchase Tax 8.25', 'purchase', ?)", (env["tax_tpl"], cid))
    conn.execute(
        "INSERT INTO tax_template_line (id, tax_template_id, tax_account_id, rate, "
        "charge_type, row_order, add_deduct) VALUES (?, ?, ?, '8.25', 'on_net_total', 0, 'add')",
        (_u(), env["tax_tpl"], env["input_tax"]))
    conn.commit()
    return env


def _po_lines(r):
    return sorted((i["item_name"], i["quantity"], i["rate"], i["amount"], i["net_amount"])
                  for i in r["items"])


def _procure(conn, env):
    """PO A 7 x 142.35 + B 3 x 19.99 with 8.25 % tax; A 4 received, then the rest."""
    env = _tax_setup(conn, env)
    po = _confirmed_po(conn, env, [("item1", "7", "142.35"), ("item2", "3", "19.99")],
                       tax_template_id=env["tax_tpl"])
    po_id = po["purchase_order_id"]
    pois = {r["item_id"]: r["id"] for r in conn.execute(
        "SELECT id, item_id FROM purchase_order_item WHERE purchase_order_id = ?",
        (po_id,)).fetchall()}
    pr1 = _receipt(conn, po_id, items=json.dumps(
        [{"purchase_order_item_id": pois[env["item1"]], "qty": "4"}]))
    return env, po, po_id, pois, pr1


def test_get_purchase_order_reads_back_totals_lines_and_progress(conn, env):
    env, po, po_id, pois, pr1 = _procure(conn, env)
    assert (po["total_amount"], po["tax_amount"], po["grand_total"]) == (
        "1056.42", "87.15", "1143.57")

    r = call_action(B.get_purchase_order, conn, ns(purchase_order_id=po_id))
    assert is_ok(r), r
    row = _row(conn, "purchase_order", po_id)
    assert (r["total_amount"], r["tax_amount"], r["grand_total"]) == ("1056.42", "87.15", "1143.57")
    assert (row["total_amount"], row["tax_amount"], row["grand_total"]) == (
        "1056.42", "87.15", "1143.57")
    assert (r["document_status"], row["status"]) == ("partially_received", "partially_received")
    assert (r["per_received"], r["per_invoiced"]) == ("40.00", "0")
    assert (row["per_received"], row["order_date"], row["tax_template_id"]) == (
        "40.00", PO_DATE, env["tax_tpl"])
    assert _po_lines(r) == [("Raw Material A", "7.00", "142.35", "996.45", "996.45"),
                            ("Raw Material B", "3.00", "19.99", "59.97", "59.97")]
    stored = {i["id"]: dict(i) for i in conn.execute(
        "SELECT * FROM purchase_order_item WHERE purchase_order_id = ?", (po_id,)).fetchall()}
    for line in r["items"]:
        for col, value in stored[line["id"]].items():
            assert line[col] == value, col
    assert Decimal(stored[pois[env["item1"]]]["received_qty"]) == Decimal("4")
    assert Decimal(stored[pois[env["item2"]]]["received_qty"]) == Decimal("0")
    assert r["purchase_receipts"] == [{"id": pr1, "naming_series": _row(
        conn, "purchase_receipt", pr1)["naming_series"], "status": "submitted",
        "posting_date": RECEIPT_DATE}]
    assert r["purchase_invoices"] == []

    pr2 = _receipt(conn, po_id, posting_date="2026-06-22")
    pi = _invoice_from_po(conn, po_id)
    pi_id = pi["purchase_invoice_id"]
    assert is_ok(call_action(B.submit_purchase_invoice, conn, ns(purchase_invoice_id=pi_id)))

    r = call_action(B.get_purchase_order, conn, ns(purchase_order_id=po_id))
    assert is_ok(r), r
    assert (r["document_status"], r["per_received"], r["per_invoiced"]) == (
        "fully_invoiced", "100.00", "100.00")
    assert _row(conn, "purchase_order", po_id)["status"] == "fully_invoiced"
    assert (r["total_amount"], r["tax_amount"], r["grand_total"]) == ("1056.42", "87.15", "1143.57")
    assert sorted((line["item_name"], Decimal(line["received_qty"]), Decimal(line["invoiced_qty"]))
                  for line in r["items"]) == [("Raw Material A", Decimal("7"), Decimal("7")),
                                              ("Raw Material B", Decimal("3"), Decimal("3"))]
    assert sorted((x["id"], x["status"], x["posting_date"]) for x in r["purchase_receipts"]) == sorted(
        [(pr1, "submitted", RECEIPT_DATE), (pr2, "submitted", "2026-06-22")])
    assert [(x["id"], x["status"], x["posting_date"], x["grand_total"], x["outstanding_amount"])
            for x in r["purchase_invoices"]] == [
        (pi_id, "submitted", INVOICE_DATE, "1143.57", "1143.57")]


def test_get_purchase_receipt_reads_back_lines_and_matches_its_ledger(conn, env):
    env, po, po_id, pois, pr1 = _procure(conn, env)
    pr2 = _receipt(conn, po_id, posting_date="2026-06-22")

    r = call_action(B.get_purchase_receipt, conn, ns(purchase_receipt_id=pr1))
    assert is_ok(r), r
    assert (r["document_status"], r["total_qty"], r["posting_date"], r["purchase_order_id"]) == (
        "submitted", "4.00", RECEIPT_DATE, po_id)
    assert [(i["item_name"], i["quantity"], i["rate"], i["amount"], i["purchase_order_item_id"])
            for i in r["items"]] == [("Raw Material A", "4.00", "142.35", "569.40",
                                      pois[env["item1"]])]
    assert _legs(_gl(conn, "purchase_receipt", pr1)) == sorted([
        (env["stock_acct"], "569.40", "0.00"), (env["srnb"], "0.00", "569.40")])

    r = call_action(B.get_purchase_receipt, conn, ns(purchase_receipt_id=pr2))
    assert is_ok(r), r
    row = _row(conn, "purchase_receipt", pr2)
    assert (r["document_status"], r["total_qty"], r["naming_series"]) == (
        "submitted", "6.00", row["naming_series"])
    assert row["total_qty"] == "6.00"
    assert sorted((i["item_name"], i["quantity"], i["rate"], i["amount"], i["purchase_order_item_id"],
                   i["warehouse_id"]) for i in r["items"]) == [
        ("Raw Material A", "3.00", "142.35", "427.05", pois[env["item1"]], env["warehouse"]),
        ("Raw Material B", "3.00", "19.99", "59.97", pois[env["item2"]], env["warehouse"])]
    stored = {i["id"]: dict(i) for i in conn.execute(
        "SELECT * FROM purchase_receipt_item WHERE purchase_receipt_id = ?", (pr2,)).fetchall()}
    for line in r["items"]:
        for col, value in stored[line["id"]].items():
            assert line[col] == value, col
    gl = _gl(conn, "purchase_receipt", pr2)
    assert _totals(gl) == ("487.02", "487.02")
    by_account = {}
    for g in gl:
        by_account[g["account_id"]] = by_account.get(g["account_id"], Decimal("0")) + (
            Decimal(g["debit"]) - Decimal(g["credit"]))
    assert {k: str(v) for k, v in by_account.items()} == {
        env["stock_acct"]: "487.02", env["srnb"]: "-487.02"}
    sle = sorted((s["item_id"], s["actual_qty"], s["stock_value_difference"]) for s in conn.execute(
        "SELECT item_id, actual_qty, stock_value_difference FROM stock_ledger_entry "
        "WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?", (pr2,)).fetchall())
    assert sle == sorted([(env["item1"], "3.00", "427.05"), (env["item2"], "3.00", "59.97")])


def test_get_purchase_invoice_reads_back_totals_lines_and_payments(conn, env):
    env, po, po_id, pois, pr1 = _procure(conn, env)
    _receipt(conn, po_id, posting_date="2026-06-22")
    pi = _invoice_from_po(conn, po_id)
    pi_id = pi["purchase_invoice_id"]
    assert (pi["total_amount"], pi["tax_amount"], pi["grand_total"]) == (
        "1056.42", "87.15", "1143.57")

    r = call_action(B.get_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
    assert is_ok(r), r
    assert (r["document_status"], r["total_amount"], r["tax_amount"], r["grand_total"],
            r["outstanding_amount"]) == ("draft", "1056.42", "87.15", "1143.57", "1143.57")
    assert r["payments"] == []

    assert is_ok(call_action(B.submit_purchase_invoice, conn, ns(purchase_invoice_id=pi_id)))
    r = call_action(B.get_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
    assert is_ok(r), r
    row = _row(conn, "purchase_invoice", pi_id)
    assert (r["document_status"], row["status"]) == ("submitted", "submitted")
    assert (r["total_amount"], r["tax_amount"], r["grand_total"], r["outstanding_amount"]) == (
        "1056.42", "87.15", "1143.57", "1143.57")
    assert (row["total_amount"], row["tax_amount"], row["grand_total"], row["outstanding_amount"]) == (
        "1056.42", "87.15", "1143.57", "1143.57")
    assert (r["posting_date"], r["due_date"], r["purchase_order_id"], r["tax_template_id"],
            r["naming_series"]) == (INVOICE_DATE, "2026-07-25", po_id, env["tax_tpl"],
                                    row["naming_series"])
    assert sorted((i["item_name"], i["quantity"], i["rate"], i["amount"], i["purchase_order_item_id"])
                  for i in r["items"]) == [
        ("Raw Material A", "7.00", "142.35", "996.45", pois[env["item1"]]),
        ("Raw Material B", "3.00", "19.99", "59.97", pois[env["item2"]])]
    assert str(sum((Decimal(i["amount"]) for i in r["items"]), Decimal("0"))) == r["total_amount"]
    assert [(p["amount"], p["party_id"], p["voucher_id"], p["account_id"]) for p in r["payments"]] == [
        ("1143.57", env["supplier"], pi_id, env["ap"])]

    gl = _gl(conn, "purchase_invoice", pi_id)
    assert _legs(gl) == sorted([
        (env["srnb"], "996.45", "0.00"), (env["srnb"], "59.97", "0.00"),
        (env["input_tax"], "87.15", "0.00"), (env["ap"], "0.00", "1143.57")])
    assert _totals(gl) == ("1143.57", "1143.57")


def test_get_actions_refuse_missing_and_unknown_ids_and_write_nothing(conn, env):
    tables = ("purchase_order", "purchase_receipt", "purchase_invoice", "gl_entry", "audit_log")
    counts = _counts(conn, *tables)
    for fn, arg, label, flag in (
            (B.get_purchase_order, "purchase_order_id", "Purchase order", "--purchase-order-id"),
            (B.get_purchase_receipt, "purchase_receipt_id", "Purchase receipt",
             "--purchase-receipt-id"),
            (B.get_purchase_invoice, "purchase_invoice_id", "Purchase invoice",
             "--purchase-invoice-id")):
        r = call_action(fn, conn, ns(**{arg: "no-such-doc"}))
        assert is_error(r)
        assert _msg(r) == f"{label} no-such-doc not found"
        r = call_action(fn, conn, ns(**{arg: None}))
        assert _msg(r) == f"{flag} is required"
    assert _counts(conn, *tables) == counts
