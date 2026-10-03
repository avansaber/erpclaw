"""PO line discount as amount, with refusals on every order entry path."""
import json
import pytest
from buying_helpers import call_action, ns, is_error, is_ok, load_db_query
from erpclaw_lib.query import P, Q, Table, fn

mod = load_db_query()


def _add_ns(env, items_json):
    return ns(supplier_id=env["supplier"], company_id=env["company_id"],
              posting_date="2026-06-15", items=items_json,
              tax_template_id=None, name=None)


def _update_ns(po_id, env, items_json):
    return ns(purchase_order_id=po_id, company_id=env["company_id"],
              items=items_json, posting_date=None,
              tax_template_id=None, supplier_id=None, name=None)


def _po_row(conn, po_id):
    t = Table("purchase_order")
    q = Q.from_(t).select(t.star).where(t.id == P())
    return conn.execute(q.get_sql(), (po_id,)).fetchone()


def _po_lines(conn, po_id):
    t = Table("purchase_order_item")
    q = Q.from_(t).select(t.star).where(t.purchase_order_id == P())
    return conn.execute(q.get_sql(), (po_id,)).fetchall()


def _audit_new(conn, action, entity_id):
    t = Table("audit_log")
    q = (Q.from_(t).select(t.new_values)
         .where(t.action == P()).where(t.entity_id == P()))
    rows = conn.execute(q.get_sql(), (action, entity_id)).fetchall()
    assert rows, "audit row missing"
    return json.loads(rows[-1]["new_values"])


def _counts(conn):
    out = {}
    for name in ("purchase_order", "purchase_order_item"):
        t = Table(name)
        q = Q.from_(t).select(fn.Count("*").as_("n"))
        out[name] = conn.execute(q.get_sql()).fetchone()["n"]
    return out


def test_amount_input_stores_net(conn, env):
    items = json.dumps([{"item_id": env["item1"], "qty": "3",
                         "rate": "10.00", "discount_amount": "1.00",
                         "warehouse_id": env["warehouse"]}])
    res = call_action(mod.add_purchase_order, conn, _add_ns(env, items))
    assert is_ok(res), res
    assert (res["total_amount"], res["grand_total"]) == ("29.00", "29.00")
    row = _po_row(conn, res["purchase_order_id"])
    assert (row["total_amount"], row["grand_total"]) == ("29.00", "29.00")
    lines = _po_lines(conn, res["purchase_order_id"])
    assert len(lines) == 1
    assert (lines[0]["amount"], lines[0]["discount_percentage"],
            lines[0]["net_amount"]) == ("30.00", "3.33", "29.00")
    assert _audit_new(conn, "add-purchase-order",
                      res["purchase_order_id"])["line_discounts"] == ["1.00"]


def test_percentage_path_unchanged(conn, env):
    items = json.dumps([{"item_id": env["item1"], "qty": "7",
                         "rate": "3.00", "discount_percentage": "12.5",
                         "warehouse_id": env["warehouse"]}])
    res = call_action(mod.add_purchase_order, conn, _add_ns(env, items))
    assert is_ok(res), res
    assert res["total_amount"] == "18.38"
    lines = _po_lines(conn, res["purchase_order_id"])
    assert (lines[0]["amount"], lines[0]["discount_percentage"],
            lines[0]["net_amount"]) == ("21.00", "12.50", "18.38")
    assert _audit_new(conn, "add-purchase-order",
                      res["purchase_order_id"])["line_discounts"] == ["2.62"]
    items2 = json.dumps([{"item_id": env["item1"], "qty": "3",
                          "rate": "10.00",
                          "discount_percentage": "3.3333333333",
                          "warehouse_id": env["warehouse"]}])
    res2 = call_action(mod.add_purchase_order, conn, _add_ns(env, items2))
    assert is_ok(res2), res2
    lines2 = _po_lines(conn, res2["purchase_order_id"])
    assert (lines2[0]["amount"], lines2[0]["discount_percentage"],
            lines2[0]["net_amount"]) == ("30.00", "3.33", "29.00")


_CASES = [
    ("both", {"qty": "3", "rate": "10.00", "discount_percentage": "5",
              "discount_amount": "1.00"},
     "Item 0: give discount_percentage or discount_amount, not both"),
    ("neg-pct", {"qty": "3", "rate": "10.00", "discount_percentage": "-1"},
     "Item 0: discount_percentage must be at least 0 and less than 100"),
    ("pct-100", {"qty": "3", "rate": "10.00", "discount_percentage": "100"},
     "Item 0: discount_percentage must be at least 0 and less than 100"),
    ("neg-amt", {"qty": "3", "rate": "10.00", "discount_amount": "-0.01"},
     "Item 0: discount_amount must not be negative"),
    ("amt-total", {"qty": "3", "rate": "10.00", "discount_amount": "30.00"},
     "Item 0: discount 30.00 must be less than the line amount 30.00"),
    ("pct-rounds-to-total", {"qty": "1", "rate": "0.01",
                             "discount_percentage": "60"},
     "Item 0: discount 0.01 must be less than the line amount 0.01"),
]


@pytest.mark.parametrize("spec,message",
                         [(c[1], c[2]) for c in _CASES],
                         ids=[c[0] for c in _CASES])
def test_refusals_write_nothing(conn, env, spec, message):
    bad_line = {"item_id": env["item1"], "warehouse_id": env["warehouse"]}
    bad_line.update(spec)
    before = _counts(conn)
    res = call_action(mod.add_purchase_order, conn,
                      _add_ns(env, json.dumps([bad_line])))
    assert is_error(res)
    assert res["message"] == message
    assert _counts(conn) == before
    good = json.dumps([{"item_id": env["item1"], "qty": "10",
                        "rate": "50.00",
                        "warehouse_id": env["warehouse"]}])
    pok = call_action(mod.add_purchase_order, conn, _add_ns(env, good))
    assert is_ok(pok), pok
    po_id = pok["purchase_order_id"]
    before2 = _counts(conn)
    survivors = [(r["item_id"], r["quantity"], r["rate"], r["amount"],
                  r["discount_percentage"], r["net_amount"])
                 for r in _po_lines(conn, po_id)]
    res2 = call_action(mod.update_purchase_order, conn,
                       _update_ns(po_id, env, json.dumps([bad_line])))
    assert is_error(res2)
    assert res2["message"] == message
    assert _counts(conn) == before2
    after = [(r["item_id"], r["quantity"], r["rate"], r["amount"],
              r["discount_percentage"], r["net_amount"])
             for r in _po_lines(conn, po_id)]
    assert after == survivors


def test_zero_amount_undiscounted_line_still_accepted(conn, env):
    items = json.dumps([{"item_id": env["item1"], "qty": "0.001",
                         "rate": "1.00",
                         "warehouse_id": env["warehouse"]}])
    res = call_action(mod.add_purchase_order, conn, _add_ns(env, items))
    assert is_ok(res), res
    lines = _po_lines(conn, res["purchase_order_id"])
    assert lines[0]["net_amount"] == "0.00"


def _mr_ns(**kw):
    defaults = dict(material_request_id=None, request_type="purchase",
                    items=None, company_id=None, supplier_id=None,
                    posting_date=None, tax_template_id=None, mr_status=None,
                    limit=None, offset=None)
    defaults.update(kw)
    return ns(**defaults)


def _make_submitted_mr(conn, env, qty="3"):
    payload = json.dumps([{"item_id": env["item1"], "qty": qty,
                           "warehouse_id": env["warehouse"]}])
    r = call_action(mod.add_material_request, conn,
                    _mr_ns(request_type="purchase", items=payload,
                           company_id=env["company_id"]))
    assert is_ok(r), r
    mr_id = r["material_request_id"]
    s = call_action(mod.submit_material_request, conn,
                    _mr_ns(material_request_id=mr_id))
    assert is_ok(s), s
    return mr_id


def test_material_request_override_amount(conn, env):
    mr_id = _make_submitted_mr(conn, env, "3")
    ov = [{"item_id": env["item1"], "qty": "3", "rate": "10.00",
           "discount_amount": "1.00"}]
    res = call_action(mod.create_po_from_material_request, conn,
                      _mr_ns(material_request_id=mr_id,
                             supplier_id=env["supplier"],
                             items=json.dumps(ov),
                             posting_date="2026-07-22"))
    assert is_ok(res), res
    assert res["items"][0]["net_amount"] == "29.00"
    lines = _po_lines(conn, res["purchase_order_id"])
    assert len(lines) == 1
    assert (lines[0]["amount"], lines[0]["discount_percentage"],
            lines[0]["net_amount"]) == ("30.00", "3.33", "29.00")
    mr2 = _make_submitted_mr(conn, env, "3")
    ov2 = [{"item_id": env["item1"], "qty": "3", "rate": "10.00",
            "discount_percentage": "5", "discount_amount": "1.00"}]
    res2 = call_action(mod.create_po_from_material_request, conn,
                       _mr_ns(material_request_id=mr2,
                              supplier_id=env["supplier"],
                              items=json.dumps(ov2),
                              posting_date="2026-07-22"))
    assert is_error(res2)
    assert res2["message"] == (
        "Item " + env["item1"] + ": give discount_percentage or "
        "discount_amount, not both")
