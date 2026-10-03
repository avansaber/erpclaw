"""Sales order status follows both deliveries and invoices (m799).

One status rule (_recompute_so_status): invoicing takes precedence over
delivery, draft/closed/cancelled never move, and cancelling the only
invoice heals the order instead of stranding it. The delivery gate lets
invoiced orders ship their remainder but never double-ships goods a
stock-moving invoice already took out.
"""
import json
from decimal import Decimal

from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_item, seed_stock_entry,
)

mod = load_db_query()


def _items(env, *specs):
    """Build order items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_confirmed_so(conn, env, items_str=None):
    items_str = items_str or _items(env, ("item1", "10", "100.00"))
    so = call_action(mod.add_sales_order, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
        delivery_date="2026-07-01", tax_template_id=None,
    ))
    assert is_ok(so), f"SO creation failed: {so}"
    submit = call_action(mod.submit_sales_order, conn, ns(
        sales_order_id=so["sales_order_id"],
    ))
    assert is_ok(submit), f"SO submit failed: {submit}"
    return so["sales_order_id"]


def _so(conn, so_id):
    return conn.execute(
        "SELECT status, per_delivered, per_invoiced FROM sales_order WHERE id=?",
        (so_id,)).fetchone()


def _create_dn(conn, so_id, items=None, posting_date="2026-06-20"):
    return call_action(mod.create_delivery_note, conn, ns(
        sales_order_id=so_id, posting_date=posting_date, items=items,
    ))


def _submit_dn(conn, dn_id):
    return call_action(mod.submit_delivery_note, conn, ns(
        delivery_note_id=dn_id,
    ))


def _si_ns(**over):
    base = dict(sales_order_id=None, delivery_note_id=None,
                customer_id=None, company_id=None,
                posting_date="2026-06-20", due_date=None,
                items=None, tax_template_id=None, payment_terms_id=None)
    base.update(over)
    return ns(**base)


def _create_si_from_so(conn, so_id, posting_date="2026-06-20"):
    return call_action(mod.create_sales_invoice, conn,
                       _si_ns(sales_order_id=so_id, posting_date=posting_date))


def _create_si_from_dn(conn, dn_id, posting_date="2026-06-21"):
    return call_action(mod.create_sales_invoice, conn,
                       _si_ns(delivery_note_id=dn_id, posting_date=posting_date))


def _submit_si(conn, si_id):
    return call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=si_id,
    ))


def _stock_balance(conn, item_id, warehouse_id):
    rows = conn.execute(
        "SELECT actual_qty FROM stock_ledger_entry "
        "WHERE item_id=? AND warehouse_id=? AND is_cancelled=0",
        (item_id, warehouse_id)).fetchall()
    total = Decimal("0")
    for r in rows:
        total += Decimal(str(r["actual_qty"]))
    return total


def _stock_issues(conn, item_id, warehouse_id):
    rows = conn.execute(
        "SELECT actual_qty FROM stock_ledger_entry "
        "WHERE item_id=? AND warehouse_id=? AND is_cancelled=0",
        (item_id, warehouse_id)).fetchall()
    return sorted(abs(Decimal(str(r["actual_qty"])))
                  for r in rows if Decimal(str(r["actual_qty"])) < 0)


def _so_audit_keys(conn, so_id):
    return [(r["action"], r["old_values"], r["new_values"])
            for r in conn.execute(
                "SELECT action, old_values, new_values FROM audit_log "
                "WHERE entity_type='sales_order' AND entity_id=?", (so_id,)).fetchall()]


def _setup_ship_rest(conn, env):
    """Order of 10 @ 50.00 (exactly 10 in stock); 4 delivered and invoiced."""
    item = seed_item(conn, "Part-ship Widget")
    seed_stock_entry(conn, item, env["warehouse"], "10", "10.00")
    items = json.dumps([{"item_id": item, "qty": "10", "rate": "50.00",
                         "warehouse_id": env["warehouse"]}])
    so_id = _create_confirmed_so(conn, env, items)

    dn1 = _create_dn(conn, so_id,
                     items=json.dumps([{"item_id": item, "qty": "4"}]))
    assert is_ok(dn1), f"DN1 creation failed: {dn1}"
    assert is_ok(_submit_dn(conn, dn1["delivery_note_id"]))

    si1 = _create_si_from_dn(conn, dn1["delivery_note_id"])
    assert is_ok(si1), f"SI1 creation failed: {si1}"
    assert is_ok(_submit_si(conn, si1["sales_invoice_id"]))

    row = _so(conn, so_id)
    assert row["status"] == "partially_invoiced", dict(row)
    return so_id, item, dn1["delivery_note_id"], si1["sales_invoice_id"]


def test_ship_rest_after_partial_invoice(conn, env):
    so_id, item, dn1_id, si1_id = _setup_ship_rest(conn, env)

    dn2 = _create_dn(conn, so_id, posting_date="2026-06-22",
                     items=json.dumps([{"item_id": item, "qty": "6"}]))
    assert is_ok(dn2), f"DN2 creation failed: {dn2}"
    assert is_ok(_submit_dn(conn, dn2["delivery_note_id"]))

    row = _so(conn, so_id)
    assert Decimal(row["per_delivered"]) == Decimal("100")
    assert Decimal(row["per_invoiced"]) == Decimal("40")
    assert row["status"] == "partially_invoiced"

    si2 = _create_si_from_dn(conn, dn2["delivery_note_id"],
                             posting_date="2026-06-23")
    assert is_ok(si2), f"SI2 creation failed: {si2}"
    assert is_ok(_submit_si(conn, si2["sales_invoice_id"]))
    assert _so(conn, so_id)["status"] == "fully_invoiced"

    assert _stock_balance(conn, item, env["warehouse"]) == Decimal("0")
    assert _stock_issues(conn, item, env["warehouse"]) == [Decimal("4"), Decimal("6")]


def test_cancel_only_invoice_resets_order(conn, env):
    so_id = _create_confirmed_so(conn, env)
    si = _create_si_from_so(conn, so_id)
    assert is_ok(si), f"invoice creation failed: {si}"
    si_id = si["sales_invoice_id"]
    db_si = conn.execute("SELECT update_stock FROM sales_invoice WHERE id=?",
                         (si_id,)).fetchone()
    assert db_si["update_stock"] == 1
    assert is_ok(_submit_si(conn, si_id))
    assert _so(conn, so_id)["status"] == "fully_invoiced"

    assert is_ok(call_action(mod.cancel_sales_invoice, conn, ns(
        sales_invoice_id=si_id)))
    row = _so(conn, so_id)
    assert Decimal(row["per_invoiced"]) == Decimal("0")
    assert row["status"] == "confirmed"

    again = _create_si_from_so(conn, so_id, posting_date="2026-06-21")
    assert is_ok(again), f"re-invoice after cancel failed: {again}"


def test_cancel_one_of_two_invoices(conn, env):
    so_id = _create_confirmed_so(conn, env)
    dn1 = _create_dn(conn, so_id, items=_items(env, ("item1", "4", "100.00")))
    assert is_ok(dn1), dn1
    assert is_ok(_submit_dn(conn, dn1["delivery_note_id"]))
    dn2 = _create_dn(conn, so_id, posting_date="2026-06-21",
                     items=_items(env, ("item1", "6", "100.00")))
    assert is_ok(dn2), dn2
    assert is_ok(_submit_dn(conn, dn2["delivery_note_id"]))
    assert _so(conn, so_id)["status"] == "fully_delivered"

    si1 = _create_si_from_dn(conn, dn1["delivery_note_id"])
    assert is_ok(si1), si1
    assert is_ok(_submit_si(conn, si1["sales_invoice_id"]))
    si2 = _create_si_from_dn(conn, dn2["delivery_note_id"],
                             posting_date="2026-06-22")
    assert is_ok(si2), si2
    assert is_ok(_submit_si(conn, si2["sales_invoice_id"]))
    assert _so(conn, so_id)["status"] == "fully_invoiced"

    assert is_ok(call_action(mod.cancel_sales_invoice, conn, ns(
        sales_invoice_id=si2["sales_invoice_id"])))
    row = _so(conn, so_id)
    assert Decimal(row["per_invoiced"]) == Decimal("40")
    assert row["status"] == "partially_invoiced"


def test_invoice_first_blocks_double_shipping(conn, env):
    so_id = _create_confirmed_so(conn, env)
    si = _create_si_from_so(conn, so_id)
    assert is_ok(si), si
    assert is_ok(_submit_si(conn, si["sales_invoice_id"]))
    before = _stock_balance(conn, env["item1"], env["warehouse"])
    assert before == Decimal("90")

    result = _create_dn(conn, so_id)
    assert is_error(result), result
    assert result["message"].startswith("Nothing left to deliver: invoice ")
    assert result["message"].endswith("already moved these goods out of stock")
    assert _stock_balance(conn, env["item1"], env["warehouse"]) == before


def test_closed_order_stays_closed(conn, env):
    so_id = _create_confirmed_so(conn, env)
    dn = _create_dn(conn, so_id, items=_items(env, ("item1", "4", "100.00")))
    assert is_ok(dn), dn
    assert is_ok(_submit_dn(conn, dn["delivery_note_id"]))
    assert _so(conn, so_id)["status"] == "partially_delivered"

    close = call_action(mod.close_sales_order, conn, ns(
        sales_order_id=so_id, reason=None, closed_by=None,
    ))
    assert is_ok(close), close

    assert is_ok(call_action(mod.cancel_delivery_note, conn, ns(
        delivery_note_id=dn["delivery_note_id"])))
    row = _so(conn, so_id)
    assert row["status"] == "closed"
    assert Decimal(row["per_delivered"]) == Decimal("0")


def test_status_change_audited(conn, env):
    so_id, item, dn1_id, si1_id = _setup_ship_rest(conn, env)

    base = _so_audit_keys(conn, so_id)
    dn2 = _create_dn(conn, so_id, posting_date="2026-06-22",
                     items=json.dumps([{"item_id": item, "qty": "6"}]))
    assert is_ok(dn2), dn2
    assert is_ok(_submit_dn(conn, dn2["delivery_note_id"]))
    assert _so_audit_keys(conn, so_id) == base

    si2 = _create_si_from_dn(conn, dn2["delivery_note_id"],
                             posting_date="2026-06-23")
    assert is_ok(si2), si2
    assert is_ok(_submit_si(conn, si2["sales_invoice_id"]))
    new_rows = [k for k in _so_audit_keys(conn, so_id) if k not in base]
    assert len(new_rows) == 1
    action, old_values, new_values = new_rows[0]
    assert action == "submit-sales-invoice"
    assert json.loads(old_values) == {"status": "partially_invoiced"}
    parsed = json.loads(new_values)
    assert parsed["status"] == "fully_invoiced"
    assert Decimal(str(parsed["per_invoiced"])) == Decimal("100")


def test_returned_goods_can_be_delivered(conn, env):
    so_id = _create_confirmed_so(conn, env)
    si = _create_si_from_so(conn, so_id)
    assert is_ok(si), si
    assert is_ok(_submit_si(conn, si["sales_invoice_id"]))

    cn = call_action(mod.create_credit_note, conn, ns(
        against_invoice_id=si["sales_invoice_id"],
        reason="Returned goods", posting_date="2026-06-25",
        items=json.dumps([{"item_id": env["item1"], "qty": "4", "rate": "100.00"}]),
    ))
    assert is_ok(cn), f"credit note creation failed: {cn}"
    assert is_ok(_submit_si(conn, cn["credit_note_id"]))
    assert _stock_balance(conn, env["item1"], env["warehouse"]) == Decimal("94")

    dn = _create_dn(conn, so_id, posting_date="2026-06-26")
    assert is_ok(dn), f"delivery of returned goods failed: {dn}"
    assert Decimal(dn["total_qty"]) == Decimal("4")
    assert is_ok(_submit_dn(conn, dn["delivery_note_id"]))

    again = _create_dn(conn, so_id, posting_date="2026-06-27")
    assert is_error(again), again
    assert again["message"].startswith("Nothing left to deliver: invoice ")
    assert again["message"].endswith("already moved these goods out of stock")
