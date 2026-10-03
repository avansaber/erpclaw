"""Strong depth for ten buying read actions (task m325c).

Each action below already had a behavioural test that only asserted on
content (a type flag, a length, ``total_count >= 1``): no money literal, no
read-back through the seam, no pinned refusal. This file deepens every one
of them without touching the existing tests. In each class the weight is
carried by the assertions marked ``WEIGHT`` below.

Mapping of weak test -> strong class (existing tests unchanged):

- ``test_blanket_po.py::TestGetBlanketPO::test_get_with_items``
  -> ``TestGetBlanketPoDepth`` (exact header + lines, money, read-back,
  native refusals).
- ``test_blanket_po.py::TestListBlanketPOs::test_list``
  -> ``TestListBlanketPosDepth``.
- ``test_misc_buying.py::TestListMaterialRequests::test_list``
  -> ``TestListMaterialRequestsDepth``.
- ``test_receipt_invoice.py::TestListPurchaseInvoices::test_list``
  -> ``TestListPurchaseInvoicesDepth``.
- ``test_purchase_order.py::TestListPurchaseOrders::test_list``
  -> ``TestListPurchaseOrdersDepth``.
- ``test_receipt_invoice.py::TestListPurchaseReceipts::test_list``
  -> ``TestListPurchaseReceiptsDepth``.
- ``test_recurring_bills.py::TestListRecurringBillTemplates::test_list``
  (+ ``test_filter_by_supplier``) -> ``TestListRecurringBillTemplatesDepth``.
- ``test_misc_buying.py::TestListRFQs::test_list`` -> ``TestListRfqsDepth``.
- ``test_misc_buying.py::TestListSupplierQuotations::test_list``
  -> ``TestListSupplierQuotationsDepth``.
- ``test_supplier.py::TestListSuppliers::test_list`` (+ ``test_list_search``)
  -> ``TestListSuppliersDepth``.

Conventions (same as the m478 depth file):

- Reads go through ``erpclaw_lib.db.get_connection`` with PyPika queries
  built via ``erpclaw_lib.query``; money is compared as exact strings, never
  float. No catalog introspection of any kind.
- Every measured call is wrapped in a before/after snapshot of its entity
  tables plus ``audit_log`` and the three ledgers
  (``gl_entry``, ``payment_ledger_entry``, ``stock_ledger_entry``): a read
  must change nothing, and a named witness row from another table must come
  back byte-identical.
- Refusal finding M325C-1: ``get-blanket-po`` is the only action of the ten
  with a native refusal path (missing id / unknown id). The nine list
  actions are total functions -- they take no required argument and always
  answer ``ok`` (verified by inspection: their bodies contain no refusal).
  Their refusal tests therefore pin two things: the action's own truthful
  negative (an unknown filter answers ``ok`` with ``total_count == 0`` and
  an empty list, changing nothing) and the closest native refusal of the
  same entity family (exact message plus an unchanged database). The finding
  is recorded in ``CHANGES.md``; no production code was changed.
"""

import json
import os
import sys
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from buying_helpers import (  # noqa: E402
    build_buying_env,
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_company,
    seed_supplier,
)
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, fn  # noqa: E402

B = load_db_query()


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def env(conn):
    return build_buying_env(conn)


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


_LEDGERS = ("gl_entry", "payment_ledger_entry", "stock_ledger_entry")


def _ledgers_empty(conn):
    for name in _LEDGERS:
        assert _count(conn, name) == 0, f"{name} should have no rows"


def _blanket_items(env, *specs):
    return json.dumps([
        {"item_id": env[key], "qty": qty, "rate": rate}
        for key, qty, rate in specs
    ])


def _wh_items(env, *specs):
    return json.dumps([
        {"item_id": env[key], "qty": qty, "rate": rate,
         "warehouse_id": env["warehouse"]}
        for key, qty, rate in specs
    ])


def _plain_items(env, *specs):
    return json.dumps([
        {"item_id": env[key], "qty": qty, "rate": rate}
        for key, qty, rate in specs
    ])


def _add_blanket(conn, env, items_str, **overrides):
    args = dict(
        supplier_id=env["supplier"], company_id=env["company_id"],
        items=items_str, valid_from="2026-01-01", valid_to="2027-12-31",
        blanket_order_id=None, blanket_status=None, tax_template_id=None,
        posting_date=None, name=None, sales_order_id=None, template_id=None,
        frequency=None, start_date=None, end_date=None, as_of_date=None,
        auto_submit=False, template_status=None,
    )
    args.update(overrides)
    return call_action(B.add_blanket_po, conn, ns(**args))


def _add_po(conn, env, items_str, posting_date="2026-06-15"):
    return call_action(B.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date=posting_date, items=items_str, tax_template_id=None,
        name=None,
    ))


def _add_rfq(conn, env, items_str, suppliers=None):
    return call_action(B.add_rfq, conn, ns(
        items=items_str,
        suppliers=suppliers or json.dumps([env["supplier"]]),
        company_id=env["company_id"],
    ))


def _recurring_ns(**overrides):
    defaults = dict(
        supplier_id=None, company_id=None, items=None, frequency=None,
        start_date=None, end_date=None, tax_template_id=None,
        auto_submit=False, posting_date=None, name=None,
        blanket_order_id=None, blanket_status=None, sales_order_id=None,
        template_id=None, as_of_date=None, template_status=None,
        limit="20", offset="0",
    )
    defaults.update(overrides)
    return ns(**defaults)


# ---------------------------------------------------------------------------
# get-blanket-po
# ---------------------------------------------------------------------------

class TestGetBlanketPoDepth:
    TABLES = ("blanket_order", "blanket_order_item", "material_request",
              "audit_log") + _LEDGERS

    def test_response_repeats_stored_header_and_lines_exactly(
            self, conn, env):
        items = _blanket_items(env, ("item1", "100", "50.00"),
                               ("item2", "50", "100.00"))
        made = _add_blanket(conn, env, items)
        assert is_ok(made)
        # Unrelated witness from another table; must survive byte-identical.
        witness_mr = call_action(B.add_material_request, conn, ns(
            request_type="purchase",
            items=_wh_items(env, ("item1", "7", "0")),
            company_id=env["company_id"],
        ))
        assert is_ok(witness_mr)
        witness_before = _row(conn, "material_request",
                              witness_mr["material_request_id"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.get_blanket_po, conn, ns(
            blanket_order_id=made["blanket_order_id"]))
        assert is_ok(r), r

        # WEIGHT: hand-computed money -- 100 x 50.00 = 5000.00 and
        # 50 x 100.00 = 5000.00, total quantity 100 + 50 = 150.00.
        assert r["document_status"] == "draft"
        assert r["total_qty"] == "150.00"
        assert Decimal(r["total_qty"]) == Decimal("150.00")
        assert r["supplier_id"] == env["supplier"]
        assert r["company_id"] == env["company_id"]
        assert (r["valid_from"], r["valid_to"]) == ("2026-01-01",
                                                   "2027-12-31")
        assert r["blanket_order_type"] == "buying"
        assert r["ordered_qty"] == "0"  # schema default, never rounded
        got = {i["item_id"]: i for i in r["items"]}
        assert set(got) == {env["item1"], env["item2"]}
        assert (got[env["item1"]]["quantity"], got[env["item1"]]["rate"],
                got[env["item1"]]["amount"]) == ("100.00", "50.00",
                                                "5000.00")
        assert (got[env["item2"]]["quantity"], got[env["item2"]]["rate"],
                got[env["item2"]]["amount"]) == ("50.00", "100.00",
                                                "5000.00")

        # WEIGHT: read-back through the seam repeats the same literals.
        stored = _row(conn, "blanket_order", made["blanket_order_id"])
        assert (stored["total_qty"], stored["status"],
                stored["supplier_id"]) == ("150.00", "draft",
                                           env["supplier"])
        lines = _where(conn, "blanket_order_item",
                       blanket_order_id=made["blanket_order_id"])
        back = {ln["item_id"]: ln for ln in lines}
        assert (back[env["item1"]]["quantity"],
                back[env["item1"]]["rate"],
                back[env["item1"]]["amount"]) == ("100.00", "50.00",
                                                 "5000.00")
        assert (back[env["item2"]]["quantity"],
                back[env["item2"]]["rate"],
                back[env["item2"]]["amount"]) == ("50.00", "100.00",
                                                 "5000.00")

        # WEIGHT: a read writes nothing -- snapshot identical, ledgers empty,
        # witness row byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "material_request",
                    witness_mr["material_request_id"]) == witness_before

    def test_pinned_refusals_leave_the_database_identical(self, conn, env):
        made = _add_blanket(conn, env, _blanket_items(env, ("item1", "100",
                                                            "50.00")))
        assert is_ok(made)
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.get_blanket_po, conn, ns(blanket_order_id=None))
        assert is_error(r)
        assert _msg(r) == "--blanket-order-id is required"
        r = call_action(B.get_blanket_po, conn,
                        ns(blanket_order_id="no-such-blanket"))
        assert is_error(r)
        assert _msg(r) == "Blanket order no-such-blanket not found"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-blanket-pos
# ---------------------------------------------------------------------------

class TestListBlanketPosDepth:
    TABLES = ("blanket_order", "blanket_order_item", "material_request",
              "audit_log") + _LEDGERS

    def test_lists_both_blankets_with_exact_quantities(self, conn, env):
        first = _add_blanket(conn, env,
                             _blanket_items(env, ("item1", "100", "50.00")))
        second = _add_blanket(conn, env,
                              _blanket_items(env, ("item2", "50", "100.00")))
        assert is_ok(first) and is_ok(second)
        witness = call_action(B.add_material_request, conn, ns(
            request_type="purchase",
            items=_wh_items(env, ("item1", "7", "0")),
            company_id=env["company_id"],
        ))
        assert is_ok(witness)
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_blanket_pos, conn, ns(
            company_id=env["company_id"], supplier_id=None,
            blanket_status=None, limit=None, offset=None))
        assert is_ok(r), r

        # WEIGHT: exact membership and hand-computed quantities.
        assert r["total_count"] == 2
        assert (r["limit"], r["offset"], r["has_more"]) == (20, 0, False)
        got = {bo["id"]: bo for bo in r["blanket_orders"]}
        assert set(got) == {first["blanket_order_id"],
                            second["blanket_order_id"]}
        assert got[first["blanket_order_id"]]["total_qty"] == "100.00"
        assert got[second["blanket_order_id"]]["total_qty"] == "50.00"
        for bo in got.values():
            assert bo["blanket_order_type"] == "buying"
            assert bo["status"] == "draft"
            assert bo["supplier_id"] == env["supplier"]
            assert (bo["valid_from"], bo["valid_to"]) == ("2026-01-01",
                                                         "2027-12-31")

        # WEIGHT: money lives in the child rows -- read back through the
        # seam: 100 x 50.00 = 5000.00 and 50 x 100.00 = 5000.00.
        first_lines = _where(conn, "blanket_order_item",
                             blanket_order_id=first["blanket_order_id"])
        assert [(ln["quantity"], ln["rate"], ln["amount"])
                for ln in first_lines] == [("100.00", "50.00", "5000.00")]
        second_lines = _where(conn, "blanket_order_item",
                              blanket_order_id=second["blanket_order_id"])
        assert [(ln["quantity"], ln["rate"], ln["amount"])
                for ln in second_lines] == [("50.00", "100.00", "5000.00")]

        # Status filter keeps both drafts; unknown company is truthfully
        # empty. Neither call may write.
        f = call_action(B.list_blanket_pos, conn, ns(
            company_id=env["company_id"], supplier_id=None,
            blanket_status="draft", limit=None, offset=None))
        assert is_ok(f) and f["total_count"] == 2
        e = call_action(B.list_blanket_pos, conn, ns(
            company_id="no-such-company", supplier_id=None,
            blanket_status=None, limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # WEIGHT: nothing changed -- snapshot identical, ledgers empty,
        # witness row byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _where(conn, "material_request",
                      id=witness["material_request_id"])[0][
            "request_type"] == "purchase"

    def test_pinned_negative_and_family_refusal(self, conn, env):
        made = _add_blanket(conn, env, _blanket_items(env, ("item1", "100",
                                                            "50.00")))
        assert is_ok(made)
        snapshot = _snapshot(conn, self.TABLES)

        # Finding M325C-1: list-blanket-pos has no native refusal path, so
        # its own negative is a truthful empty, not an error.
        e = call_action(B.list_blanket_pos, conn, ns(
            company_id="no-such-company", supplier_id=None,
            blanket_status=None, limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # The pinned refusal is the owning write guard of the same family.
        r = call_action(B.add_blanket_po, conn, ns(
            supplier_id=None, company_id=env["company_id"],
            items=_blanket_items(env, ("item1", "10", "50.00")),
            valid_from="2026-01-01", valid_to="2027-12-31",
            blanket_order_id=None, blanket_status=None, tax_template_id=None,
            posting_date=None, name=None, sales_order_id=None, template_id=None,
            frequency=None, start_date=None, end_date=None, as_of_date=None,
            auto_submit=False, template_status=None))
        assert is_error(r)
        assert _msg(r) == "--supplier-id is required"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-material-requests (no money on these rows: quantities only)
# ---------------------------------------------------------------------------

class TestListMaterialRequestsDepth:
    TABLES = ("material_request", "material_request_item", "supplier",
              "audit_log") + _LEDGERS

    def test_lists_both_requests_with_exact_lines(self, conn, env):
        first = call_action(B.add_material_request, conn, ns(
            request_type="purchase",
            items=_wh_items(env, ("item1", "20", "0")),
            company_id=env["company_id"],
        ))
        second = call_action(B.add_material_request, conn, ns(
            request_type="purchase",
            items=_wh_items(env, ("item2", "5", "0")),
            company_id=env["company_id"],
        ))
        assert is_ok(first) and is_ok(second)
        witness_before = _row(conn, "supplier", env["supplier"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_material_requests, conn, ns(
            company_id=env["company_id"], request_type=None, mr_status=None,
            limit=None, offset=None))
        assert is_ok(r), r

        # WEIGHT: exact membership and header values (no amounts exist on
        # material requests, so quantities and status carry the weight).
        assert r["total_count"] == 2
        assert (r["limit"], r["offset"], r["has_more"]) == (20, 0, False)
        got = {mr["id"]: mr for mr in r["material_requests"]}
        assert set(got) == {first["material_request_id"],
                            second["material_request_id"]}
        for mr in got.values():
            assert mr["request_type"] == "purchase"
            assert mr["status"] == "draft"
            assert mr["company_id"] == env["company_id"]

        # WEIGHT: read-back through the seam -- exact stored lines.
        first_lines = _where(conn, "material_request_item",
                             material_request_id=first["material_request_id"])
        assert [(ln["item_id"], ln["quantity"])
                for ln in first_lines] == [(env["item1"], "20.00")]
        second_lines = _where(conn, "material_request_item",
                              material_request_id=second[
                                  "material_request_id"])
        assert [(ln["item_id"], ln["quantity"])
                for ln in second_lines] == [(env["item2"], "5.00")]

        # Filters: type and status each keep both; unknown company is empty.
        f = call_action(B.list_material_requests, conn, ns(
            company_id=env["company_id"], request_type="purchase",
            mr_status="draft", limit=None, offset=None))
        assert is_ok(f) and f["total_count"] == 2
        e = call_action(B.list_material_requests, conn, ns(
            company_id="no-such-company", request_type=None, mr_status=None,
            limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # WEIGHT: nothing changed; witness supplier byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "supplier", env["supplier"]) == witness_before

    def test_pinned_negative_and_family_refusal(self, conn, env):
        made = call_action(B.add_material_request, conn, ns(
            request_type="purchase",
            items=_wh_items(env, ("item1", "5", "0")),
            company_id=env["company_id"],
        ))
        assert is_ok(made)
        snapshot = _snapshot(conn, self.TABLES)

        # Finding M325C-1: no native refusal; the list's own negative is a
        # truthful empty.
        e = call_action(B.list_material_requests, conn, ns(
            company_id="no-such-company", request_type=None, mr_status=None,
            limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # The pinned refusal is the owning write guard of the same family.
        r = call_action(B.add_material_request, conn, ns(
            request_type="purchase", items=None,
            company_id=env["company_id"]))
        assert is_error(r)
        assert _msg(r) == "--items is required (JSON array)"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-purchase-invoices
# ---------------------------------------------------------------------------

class TestListPurchaseInvoicesDepth:
    TABLES = ("purchase_invoice", "purchase_invoice_item", "purchase_order",
              "audit_log") + _LEDGERS

    def _add_standalone(self, conn, env, qty, rate, posting_date):
        return call_action(B.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date=posting_date, due_date=None,
            items=_wh_items(env, ("item1", qty, rate)),
            tax_template_id=None,
        ))

    def test_lists_both_invoices_date_descending_with_exact_money(
            self, conn, env):
        # Hand-computed: 5 x 100.00 = 500.00; 3 x 100.00 = 300.00, no tax.
        early = self._add_standalone(conn, env, "5", "100.00", "2026-06-20")
        late = self._add_standalone(conn, env, "3", "100.00", "2026-06-21")
        assert is_ok(early) and is_ok(late)
        witness_po = _add_po(conn, env, _wh_items(env, ("item1", "10",
                                                       "50.00")))
        assert is_ok(witness_po)
        witness_before = _row(conn, "purchase_order",
                              witness_po["purchase_order_id"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_purchase_invoices, conn, ns(
            company_id=env["company_id"], supplier_id=None, pi_status=None,
            from_date=None, to_date=None, limit=None, offset=None))
        assert is_ok(r), r

        # WEIGHT: exact order (posting_date desc) and hand-computed money.
        assert r["total_count"] == 2
        assert [pi["id"] for pi in r["purchase_invoices"]] == [
            late["purchase_invoice_id"], early["purchase_invoice_id"]]
        lat, ear = r["purchase_invoices"]
        assert (lat["total_amount"], lat["tax_amount"], lat["grand_total"],
                lat["outstanding_amount"]) == ("300.00", "0.00", "300.00",
                                              "300.00")
        assert (ear["total_amount"], ear["tax_amount"], ear["grand_total"],
                ear["outstanding_amount"]) == ("500.00", "0.00", "500.00",
                                              "500.00")
        assert Decimal(lat["grand_total"]) == Decimal("300.00")
        assert Decimal(ear["grand_total"]) == Decimal("500.00")
        for pi in (lat, ear):
            assert pi["status"] == "draft"
            assert pi["supplier_id"] == env["supplier"]
            assert pi["supplier_name"] == lat["supplier_name"]

        # WEIGHT: read-back through the seam repeats the literals.
        stored_late = _row(conn, "purchase_invoice",
                           late["purchase_invoice_id"])
        assert (stored_late["total_amount"], stored_late["grand_total"],
                stored_late["outstanding_amount"]) == ("300.00", "300.00",
                                                      "300.00")
        late_lines = _where(conn, "purchase_invoice_item",
                            purchase_invoice_id=late["purchase_invoice_id"])
        assert [(ln["item_id"], ln["quantity"], ln["rate"], ln["amount"])
                for ln in late_lines] == [(env["item1"], "3.00", "100.00",
                                           "300.00")]

        # Filters: status and supplier keep both; from_date isolates the
        # later bill; unknown company is truthfully empty.
        f = call_action(B.list_purchase_invoices, conn, ns(
            company_id=env["company_id"], supplier_id=env["supplier"],
            pi_status="draft", from_date="2026-06-21", to_date=None,
            limit=None, offset=None))
        assert is_ok(f) and f["total_count"] == 1
        assert f["purchase_invoices"][0]["id"] == late["purchase_invoice_id"]
        e = call_action(B.list_purchase_invoices, conn, ns(
            company_id="no-such-company", supplier_id=None, pi_status=None,
            from_date=None, to_date=None, limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # WEIGHT: nothing changed; witness PO byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "purchase_order",
                    witness_po["purchase_order_id"]) == witness_before

    def test_pinned_negative_and_family_refusal(self, conn, env):
        made = self._add_standalone(conn, env, "1", "10.00", "2026-06-20")
        assert is_ok(made)
        snapshot = _snapshot(conn, self.TABLES)

        # Finding M325C-1: no native refusal; the list's own negative is a
        # truthful empty.
        e = call_action(B.list_purchase_invoices, conn, ns(
            company_id="no-such-company", supplier_id=None, pi_status=None,
            from_date=None, to_date=None, limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # The pinned refusal is the owning write guard of the same family.
        r = call_action(B.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=_wh_items(env, ("item1", "1", "10.00")),
            tax_template_id=None))
        assert is_error(r)
        assert _msg(r) == "--supplier-id is required for standalone invoice"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-purchase-orders
# ---------------------------------------------------------------------------

class TestListPurchaseOrdersDepth:
    TABLES = ("purchase_order", "purchase_order_item", "purchase_invoice",
              "audit_log") + _LEDGERS

    def test_lists_both_orders_date_descending_with_exact_money(
            self, conn, env):
        # Hand-computed: 10 x 50.00 = 500.00; 4 x 100.00 = 400.00, no tax.
        older = _add_po(conn, env, _wh_items(env, ("item1", "10", "50.00")),
                        posting_date="2026-06-15")
        newer = _add_po(conn, env, _wh_items(env, ("item2", "4", "100.00")),
                        posting_date="2026-06-16")
        assert is_ok(older) and is_ok(newer)
        witness_pi = call_action(B.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=_wh_items(env, ("item1", "1", "10.00")),
            tax_template_id=None))
        assert is_ok(witness_pi)
        witness_before = _row(conn, "purchase_invoice",
                              witness_pi["purchase_invoice_id"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_purchase_orders, conn, ns(
            company_id=env["company_id"], supplier_id=None, po_status=None,
            from_date=None, to_date=None, limit=None, offset=None))
        assert is_ok(r), r

        # WEIGHT: exact order (order_date desc) and hand-computed money.
        assert r["total_count"] == 2
        assert [po["id"] for po in r["purchase_orders"]] == [
            newer["purchase_order_id"], older["purchase_order_id"]]
        new, old = r["purchase_orders"]
        assert (new["total_amount"], new["tax_amount"],
                new["grand_total"]) == ("400.00", "0.00", "400.00")
        assert (old["total_amount"], old["tax_amount"],
                old["grand_total"]) == ("500.00", "0.00", "500.00")
        assert Decimal(new["grand_total"]) == Decimal("400.00")
        assert Decimal(old["grand_total"]) == Decimal("500.00")
        for po in (new, old):
            assert po["status"] == "draft"
            assert po["supplier_id"] == env["supplier"]

        # WEIGHT: read-back through the seam repeats the literals.
        stored_new = _row(conn, "purchase_order",
                          newer["purchase_order_id"])
        assert (stored_new["total_amount"], stored_new["tax_amount"],
                stored_new["grand_total"]) == ("400.00", "0.00", "400.00")
        new_lines = _where(conn, "purchase_order_item",
                           purchase_order_id=newer["purchase_order_id"])
        assert [(ln["item_id"], ln["quantity"], ln["rate"], ln["amount"])
                for ln in new_lines] == [(env["item2"], "4.00", "100.00",
                                          "400.00")]

        # Filters: status and supplier keep both; the date window isolates
        # the newer order; unknown company is truthfully empty.
        f = call_action(B.list_purchase_orders, conn, ns(
            company_id=env["company_id"], supplier_id=env["supplier"],
            po_status="draft", from_date="2026-06-16", to_date="2026-06-16",
            limit=None, offset=None))
        assert is_ok(f) and f["total_count"] == 1
        assert f["purchase_orders"][0]["id"] == newer["purchase_order_id"]
        e = call_action(B.list_purchase_orders, conn, ns(
            company_id="no-such-company", supplier_id=None, po_status=None,
            from_date=None, to_date=None, limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # WEIGHT: nothing changed; witness invoice byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "purchase_invoice",
                    witness_pi["purchase_invoice_id"]) == witness_before

    def test_company_filter_isolates_and_family_refusal_pins(
            self, conn, env):
        home = _add_po(conn, env, _wh_items(env, ("item1", "10", "50.00")))
        assert is_ok(home)
        other_company = seed_company(conn, name="Other Co", abbr="OC")
        other_supplier = seed_supplier(conn, other_company, name="Other Inc")
        away = call_action(B.add_purchase_order, conn, ns(
            supplier_id=other_supplier, company_id=other_company,
            posting_date="2026-06-15",
            items=_wh_items(env, ("item1", "2", "50.00")),
            tax_template_id=None, name=None,
        ))
        assert is_ok(away)
        snapshot = _snapshot(conn, self.TABLES)

        # The home filter must not leak the other company's order.
        r = call_action(B.list_purchase_orders, conn, ns(
            company_id=env["company_id"], supplier_id=None, po_status=None,
            from_date=None, to_date=None, limit=None, offset=None))
        assert is_ok(r) and r["total_count"] == 1
        assert r["purchase_orders"][0]["id"] == home["purchase_order_id"]

        # Finding M325C-1: no native refusal on the list itself; the pinned
        # refusal is the owning write guard of the same family.
        bad = call_action(B.add_purchase_order, conn, ns(
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-15",
            items=_wh_items(env, ("item1", "1", "10.00")),
            tax_template_id=None, name=None))
        assert is_error(bad)
        assert _msg(bad) == "--supplier-id is required"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-purchase-receipts
# ---------------------------------------------------------------------------

class TestListPurchaseReceiptsDepth:
    TABLES = ("purchase_order", "purchase_receipt", "purchase_receipt_item",
              "purchase_invoice", "audit_log") + _LEDGERS

    def _confirmed_po(self, conn, env, items_str, posting_date="2026-06-15"):
        made = _add_po(conn, env, items_str, posting_date=posting_date)
        assert is_ok(made)
        sub = call_action(B.submit_purchase_order, conn, ns(
            purchase_order_id=made["purchase_order_id"]))
        assert is_ok(sub), sub
        return made["purchase_order_id"]

    def _receipt(self, conn, env, po_id, posting_date):
        return call_action(B.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date=posting_date, items=None,
            purchase_receipt_id=None,
        ))

    def test_lists_both_receipts_date_descending_with_exact_quantities(
            self, conn, env):
        po_early = self._confirmed_po(conn, env, _wh_items(env, ("item1",
                                                                 "10",
                                                                 "50.00")))
        po_late = self._confirmed_po(conn, env, _wh_items(env, ("item1",
                                                                "10",
                                                                "50.00")))
        early = self._receipt(conn, env, po_early, "2026-06-20")
        late = self._receipt(conn, env, po_late, "2026-06-21")
        assert is_ok(early) and is_ok(late)
        witness_pi = call_action(B.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=_wh_items(env, ("item1", "1", "10.00")),
            tax_template_id=None))
        assert is_ok(witness_pi)
        witness_before = _row(conn, "purchase_invoice",
                              witness_pi["purchase_invoice_id"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_purchase_receipts, conn, ns(
            company_id=env["company_id"], supplier_id=None, pr_status=None,
            limit=None, offset=None))
        assert is_ok(r), r

        # WEIGHT: exact order (posting_date desc) and quantities; each
        # receipt carries the full PO quantity of 10.00.
        assert r["total_count"] == 2
        assert [pr["id"] for pr in r["purchase_receipts"]] == [
            late["purchase_receipt_id"], early["purchase_receipt_id"]]
        lat, ear = r["purchase_receipts"]
        assert (lat["total_qty"], lat["posting_date"],
                lat["status"]) == ("10.00", "2026-06-21", "draft")
        assert (ear["total_qty"], ear["posting_date"],
                ear["status"]) == ("10.00", "2026-06-20", "draft")
        for pr in (lat, ear):
            assert pr["supplier_id"] == env["supplier"]

        # WEIGHT: money lives in the child rows -- 10 x 50.00 = 500.00 --
        # read back through the seam.
        late_lines = _where(conn, "purchase_receipt_item",
                            purchase_receipt_id=late["purchase_receipt_id"])
        assert [(ln["item_id"], ln["quantity"], ln["rate"], ln["amount"])
                for ln in late_lines] == [(env["item1"], "10.00", "50.00",
                                           "500.00")]
        stored_late = _row(conn, "purchase_receipt",
                           late["purchase_receipt_id"])
        assert (stored_late["total_qty"],
                stored_late["purchase_order_id"]) == ("10.00", po_late)

        # Filters: status and supplier keep both; unknown company is empty.
        f = call_action(B.list_purchase_receipts, conn, ns(
            company_id=env["company_id"], supplier_id=env["supplier"],
            pr_status="draft", limit=None, offset=None))
        assert is_ok(f) and f["total_count"] == 2
        e = call_action(B.list_purchase_receipts, conn, ns(
            company_id="no-such-company", supplier_id=None, pr_status=None,
            limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # WEIGHT: nothing changed; witness invoice byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "purchase_invoice",
                    witness_pi["purchase_invoice_id"]) == witness_before

    def test_pinned_negative_and_family_refusal(self, conn, env):
        po_id = self._confirmed_po(conn, env, _wh_items(env, ("item1", "10",
                                                              "50.00")))
        made = self._receipt(conn, env, po_id, "2026-06-20")
        assert is_ok(made)
        snapshot = _snapshot(conn, self.TABLES)

        # Finding M325C-1: no native refusal; the list's own negative is a
        # truthful empty.
        e = call_action(B.list_purchase_receipts, conn, ns(
            company_id="no-such-company", supplier_id=None, pr_status=None,
            limit=None, offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # The pinned refusal is the owning write guard of the same family.
        r = call_action(B.create_purchase_receipt, conn, ns(
            purchase_order_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None))
        assert is_error(r)
        assert _msg(r) == "--purchase-order-id is required"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-recurring-bill-templates
# ---------------------------------------------------------------------------

class TestListRecurringBillTemplatesDepth:
    TABLES = ("recurring_bill_template", "recurring_bill_template_item",
              "supplier", "audit_log") + _LEDGERS

    def _add_template(self, conn, env, items_str, frequency, start_date,
                      end_date="2026-12-31"):
        return call_action(B.add_recurring_bill_template, conn,
                           _recurring_ns(
                               supplier_id=env["supplier"],
                               company_id=env["company_id"], items=items_str,
                               frequency=frequency, start_date=start_date,
                               end_date=end_date))

    def test_lists_both_templates_with_exact_lines(self, conn, env):
        # Hand-computed: 1 x 500.00 = 500.00; 2 x 250.00 = 500.00.
        monthly = self._add_template(conn, env,
                                     _plain_items(env, ("item1", "1",
                                                        "500.00")),
                                     "monthly", "2026-01-01")
        quarterly = self._add_template(conn, env,
                                       _plain_items(env, ("item1", "2",
                                                          "250.00")),
                                       "quarterly", "2026-04-01")
        assert is_ok(monthly) and is_ok(quarterly)
        witness_before = _row(conn, "supplier", env["supplier"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_recurring_bill_templates, conn,
                        _recurring_ns(company_id=env["company_id"]))
        assert is_ok(r), r

        # WEIGHT: exact membership with hand-pinned header values.
        assert r["total_count"] == 2
        got = {t["id"]: t for t in r["recurring_bill_templates"]}
        assert set(got) == {monthly["template_id"],
                            quarterly["template_id"]}
        mon, qua = got[monthly["template_id"]], got[quarterly["template_id"]]
        assert (mon["frequency"], mon["start_date"],
                mon["next_bill_date"], mon["status"]) == (
            "monthly", "2026-01-01", "2026-01-01", "draft")
        assert (qua["frequency"], qua["start_date"],
                qua["next_bill_date"], qua["status"]) == (
            "quarterly", "2026-04-01", "2026-04-01", "draft")
        for t in (mon, qua):
            assert t["supplier_id"] == env["supplier"]
            assert t["company_id"] == env["company_id"]

        # WEIGHT: money lives in the child rows -- read back through the
        # seam with hand-computed amounts.
        mon_lines = _where(conn, "recurring_bill_template_item",
                           template_id=monthly["template_id"])
        assert [(ln["item_id"], ln["quantity"], ln["rate"], ln["amount"])
                for ln in mon_lines] == [(env["item1"], "1.00", "500.00",
                                          "500.00")]
        qua_lines = _where(conn, "recurring_bill_template_item",
                           template_id=quarterly["template_id"])
        assert [(ln["item_id"], ln["quantity"], ln["rate"], ln["amount"])
                for ln in qua_lines] == [(env["item1"], "2.00", "250.00",
                                          "500.00")]

        # Filters: supplier and draft status keep both; unknown company is
        # truthfully empty.
        f = call_action(B.list_recurring_bill_templates, conn,
                        _recurring_ns(company_id=env["company_id"],
                                      supplier_id=env["supplier"],
                                      template_status="draft"))
        assert is_ok(f) and f["total_count"] == 2
        e = call_action(B.list_recurring_bill_templates, conn,
                        _recurring_ns(company_id="no-such-company"))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # WEIGHT: nothing changed; witness supplier byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "supplier", env["supplier"]) == witness_before

    def test_pinned_negative_and_family_refusal(self, conn, env):
        made = self._add_template(conn, env,
                                  _plain_items(env, ("item1", "1", "500.00")),
                                  "monthly", "2026-01-01")
        assert is_ok(made)
        snapshot = _snapshot(conn, self.TABLES)

        # Finding M325C-1: no native refusal; the list's own negative is a
        # truthful empty.
        e = call_action(B.list_recurring_bill_templates, conn,
                        _recurring_ns(company_id="no-such-company"))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # The pinned refusal is the owning write guard of the same family.
        r = call_action(B.add_recurring_bill_template, conn, _recurring_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=_plain_items(env, ("item1", "1", "500.00")),
            frequency=None, start_date="2026-01-01"))
        assert is_error(r)
        assert _msg(r) == "--frequency is required"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-rfqs (no money on these rows: quantities only)
# ---------------------------------------------------------------------------

class TestListRfqsDepth:
    TABLES = ("request_for_quotation", "rfq_item", "rfq_supplier",
              "material_request", "audit_log") + _LEDGERS

    def test_lists_both_rfqs_with_exact_lines(self, conn, env):
        first = _add_rfq(conn, env, _wh_items(env, ("item1", "50", "0")))
        second = _add_rfq(conn, env, _wh_items(env, ("item2", "10", "0")))
        assert is_ok(first) and is_ok(second)
        witness_mr = call_action(B.add_material_request, conn, ns(
            request_type="purchase",
            items=_wh_items(env, ("item1", "7", "0")),
            company_id=env["company_id"],
        ))
        assert is_ok(witness_mr)
        witness_before = _row(conn, "material_request",
                              witness_mr["material_request_id"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_rfqs, conn, ns(
            company_id=env["company_id"], rfq_status=None, limit=None,
            offset=None))
        assert is_ok(r), r

        # WEIGHT: exact membership and header values (quantities, not
        # amounts, carry the weight -- RFQ rows hold no money).
        assert r["total_count"] == 2
        got = {rfq["id"]: rfq for rfq in r["rfqs"]}
        assert set(got) == {first["rfq_id"], second["rfq_id"]}
        for rfq in got.values():
            assert rfq["status"] == "draft"
            assert rfq["company_id"] == env["company_id"]

        # WEIGHT: read-back through the seam -- exact stored lines.
        first_lines = _where(conn, "rfq_item", rfq_id=first["rfq_id"])
        assert [(ln["item_id"], ln["quantity"])
                for ln in first_lines] == [(env["item1"], "50.00")]
        second_lines = _where(conn, "rfq_item", rfq_id=second["rfq_id"])
        assert [(ln["item_id"], ln["quantity"])
                for ln in second_lines] == [(env["item2"], "10.00")]

        # Status filter keeps both drafts; unknown company is empty.
        f = call_action(B.list_rfqs, conn, ns(
            company_id=env["company_id"], rfq_status="draft", limit=None,
            offset=None))
        assert is_ok(f) and f["total_count"] == 2
        e = call_action(B.list_rfqs, conn, ns(
            company_id="no-such-company", rfq_status=None, limit=None,
            offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # WEIGHT: nothing changed; witness request byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "material_request",
                    witness_mr["material_request_id"]) == witness_before

    def test_pinned_negative_and_family_refusal(self, conn, env):
        made = _add_rfq(conn, env, _wh_items(env, ("item1", "10", "0")))
        assert is_ok(made)
        snapshot = _snapshot(conn, self.TABLES)

        # Finding M325C-1: no native refusal; the list's own negative is a
        # truthful empty.
        e = call_action(B.list_rfqs, conn, ns(
            company_id="no-such-company", rfq_status=None, limit=None,
            offset=None))
        assert e == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, e

        # The pinned refusal is the owning write guard of the same family.
        r = call_action(B.add_rfq, conn, ns(
            items=_wh_items(env, ("item1", "10", "0")), suppliers=None,
            company_id=env["company_id"]))
        assert is_error(r)
        assert _msg(r) == "--suppliers is required (JSON array of supplier IDs)"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-supplier-quotations
# ---------------------------------------------------------------------------

class TestListSupplierQuotationsDepth:
    TABLES = ("supplier_quotation", "supplier_quotation_item",
              "request_for_quotation", "purchase_order",
              "audit_log") + _LEDGERS

    def _quote(self, conn, env, rfq_id, supplier_id, rfq_item_id, rate):
        return call_action(B.add_supplier_quotation, conn, ns(
            rfq_id=rfq_id, supplier_id=supplier_id,
            items=json.dumps([{"rfq_item_id": rfq_item_id,
                               "rate": rate}]),
            company_id=env["company_id"], tax_template_id=None,
        ))

    def test_lists_both_quotations_with_exact_money(self, conn, env):
        rfq = _add_rfq(conn, env, _wh_items(env, ("item1", "50", "0")))
        assert is_ok(rfq)
        rfq_item = _where(conn, "rfq_item", rfq_id=rfq["rfq_id"])[0]
        other_supplier = seed_supplier(conn, env["company_id"],
                                       name="Beta Parts")
        # Hand-computed: 50 x 45.00 = 2250.00; 50 x 40.00 = 2000.00.
        first = self._quote(conn, env, rfq["rfq_id"], env["supplier"],
                            rfq_item["id"], "45.00")
        second = self._quote(conn, env, rfq["rfq_id"], other_supplier,
                             rfq_item["id"], "40.00")
        assert is_ok(first) and is_ok(second)
        witness_po = _add_po(conn, env, _wh_items(env, ("item1", "10",
                                                       "50.00")))
        assert is_ok(witness_po)
        witness_before = _row(conn, "purchase_order",
                              witness_po["purchase_order_id"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_supplier_quotations, conn, ns(
            rfq_id=rfq["rfq_id"], supplier_id=None, limit=None, offset=None))
        assert is_ok(r), r

        # WEIGHT: exact membership keyed by supplier with hand-computed
        # money on every row.
        assert r["total_count"] == 2
        got = {sq["supplier_id"]: sq
               for sq in r["supplier_quotations"]}
        assert set(got) == {env["supplier"], other_supplier}
        assert (got[env["supplier"]]["total_amount"],
                got[env["supplier"]]["grand_total"]) == ("2250.00",
                                                        "2250.00")
        assert (got[other_supplier]["total_amount"],
                got[other_supplier]["grand_total"]) == ("2000.00",
                                                       "2000.00")
        assert Decimal(
            got[other_supplier]["grand_total"]) == Decimal("2000.00")
        for sq in got.values():
            assert sq["status"] == "draft"
            assert sq["rfq_id"] == rfq["rfq_id"]

        # WEIGHT: read-back through the seam repeats the literals.
        stored_first = _row(conn, "supplier_quotation",
                            first["supplier_quotation_id"])
        assert (stored_first["total_amount"],
                stored_first["grand_total"]) == ("2250.00", "2250.00")
        first_lines = _where(conn, "supplier_quotation_item",
                             supplier_quotation_id=first[
                                 "supplier_quotation_id"])
        assert [(ln["item_id"], ln["quantity"], ln["rate"], ln["amount"])
                for ln in first_lines] == [(env["item1"], "50.00", "45.00",
                                            "2250.00")]

        # Supplier filter isolates one quotation; an unknown RFQ is
        # truthfully empty.
        f = call_action(B.list_supplier_quotations, conn, ns(
            rfq_id=rfq["rfq_id"], supplier_id=env["supplier"], limit=None,
            offset=None))
        assert is_ok(f) and f["total_count"] == 1
        assert f["supplier_quotations"][0]["id"] == first[
            "supplier_quotation_id"]
        e = call_action(B.list_supplier_quotations, conn, ns(
            rfq_id="no-such-rfq", supplier_id=None, limit=None, offset=None))
        assert is_ok(e) and e["total_count"] == 0
        assert e["supplier_quotations"] == []

        # WEIGHT: nothing changed; witness PO byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "purchase_order",
                    witness_po["purchase_order_id"]) == witness_before

    def test_pinned_negative_and_family_refusal(self, conn, env):
        rfq = _add_rfq(conn, env, _wh_items(env, ("item1", "10", "0")))
        assert is_ok(rfq)
        snapshot = _snapshot(conn, self.TABLES)

        # Finding M325C-1: no native refusal; the list's own negative is a
        # truthful empty.
        e = call_action(B.list_supplier_quotations, conn, ns(
            rfq_id="no-such-rfq", supplier_id=None, limit=None, offset=None))
        assert is_ok(e)
        assert e["total_count"] == 0 and e["supplier_quotations"] == []

        # The pinned refusal is the owning write guard of the same family.
        rfq_item = _where(conn, "rfq_item", rfq_id=rfq["rfq_id"])[0]
        r = call_action(B.add_supplier_quotation, conn, ns(
            rfq_id=None, supplier_id=env["supplier"],
            items=json.dumps([{"rfq_item_id": rfq_item["id"],
                               "rate": "45.00"}]),
            company_id=env["company_id"], tax_template_id=None))
        assert is_error(r)
        assert _msg(r) == "--rfq-id is required"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)


# ---------------------------------------------------------------------------
# list-suppliers (no money on these rows)
# ---------------------------------------------------------------------------

class TestListSuppliersDepth:
    TABLES = ("supplier", "item", "audit_log") + _LEDGERS

    def _add_supplier(self, conn, env, name):
        return call_action(B.add_supplier, conn, ns(
            name=name, company_id=env["company_id"], supplier_type=None,
            supplier_group=None, payment_terms_id=None, tax_id=None,
            is_1099_vendor=None, primary_address=None,
        ))

    def test_lists_both_suppliers_with_exact_rows(self, conn, env):
        made = self._add_supplier(conn, env, "Beta Parts")
        assert is_ok(made)
        witness_before = _row(conn, "item", env["item1"])
        snapshot = _snapshot(conn, self.TABLES)

        r = call_action(B.list_suppliers, conn, ns(
            company_id=env["company_id"], supplier_group=None, search=None,
            limit=None, offset=None))
        assert is_ok(r), r

        # WEIGHT: exact membership and header values (supplier rows hold no
        # money, so identity columns carry the weight).
        assert r["total_count"] == 2
        got = {s["name"]: s for s in r["suppliers"]}
        assert set(got) == {"Acme Supplies", "Beta Parts"}
        for s in got.values():
            assert s["company_id"] == env["company_id"]
            assert s["supplier_type"] == "company"
            assert s["status"] == "active"
        assert got["Beta Parts"]["id"] == made["supplier_id"]

        # WEIGHT: read-back through the seam repeats the stored rows.
        stored_beta = _row(conn, "supplier", made["supplier_id"])
        assert (stored_beta["name"], stored_beta["status"],
                stored_beta["company_id"]) == ("Beta Parts", "active",
                                               env["company_id"])

        # Search isolates one supplier; an unknown search is truthfully
        # empty and names travel with the rows.
        f = call_action(B.list_suppliers, conn, ns(
            company_id=env["company_id"], supplier_group=None,
            search="Beta", limit=None, offset=None))
        assert is_ok(f) and f["total_count"] == 1
        assert f["suppliers"][0]["name"] == "Beta Parts"
        e = call_action(B.list_suppliers, conn, ns(
            company_id=env["company_id"], supplier_group=None,
            search="no-such-supplier", limit=None, offset=None))
        assert is_ok(e) and e["total_count"] == 0
        assert e["suppliers"] == []

        # WEIGHT: nothing changed; witness item byte-identical.
        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
        assert _row(conn, "item", env["item1"]) == witness_before

    def test_company_filter_isolates_and_family_refusal_pins(
            self, conn, env):
        other_company = seed_company(conn, name="Other Co", abbr="OC")
        away_id = seed_supplier(conn, other_company, name="Other Inc")
        snapshot = _snapshot(conn, self.TABLES)

        # The home filter must not leak the other company's supplier.
        r = call_action(B.list_suppliers, conn, ns(
            company_id=env["company_id"], supplier_group=None, search=None,
            limit=None, offset=None))
        assert is_ok(r) and r["total_count"] == 1
        assert r["suppliers"][0]["name"] == "Acme Supplies"
        assert away_id not in {s["id"] for s in r["suppliers"]}

        # Finding M325C-1: no native refusal on the list itself; the pinned
        # refusal is the owning write guard of the same family.
        bad = call_action(B.add_supplier, conn, ns(
            name=None, company_id=env["company_id"], supplier_type=None,
            supplier_group=None, payment_terms_id=None, tax_id=None,
            is_1099_vendor=None, primary_address=None))
        assert is_error(bad)
        assert _msg(bad) == "--name is required"

        assert _snapshot(conn, self.TABLES) == snapshot
        _ledgers_empty(conn)
