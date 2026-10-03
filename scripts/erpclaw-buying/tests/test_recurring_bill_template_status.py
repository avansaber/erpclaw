"""Tests for update-recurring-bill-template (activate / pause / cancel).

Covers the draft-then-activate lifecycle: a template created through
add-recurring-bill-template starts as 'draft' and generates nothing until
update-recurring-bill-template flips it to 'active'.
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


def _common_ns(**overrides):
    """Build namespace with common defaults for recurring bill actions."""
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


def _add_monthly_template(conn, env, start="2026-09-01", end=None, **specs):
    kwargs = dict(
        supplier_id=env["supplier"], company_id=env["company_id"],
        items=_items(env, ("item1", "1", "1000.00")), frequency="monthly",
        start_date=start,
    )
    if end is not None:
        kwargs["end_date"] = end
    kwargs.update(specs)
    result = call_action(mod.add_recurring_bill_template, conn, _common_ns(**kwargs))
    assert is_ok(result)
    return result["template_id"]


def _read_template(conn, template_id):
    row = conn.execute(
        "SELECT * FROM recurring_bill_template WHERE id = ?",
        (template_id,)).fetchone()
    return dict(row) if row else None


def _read_items(conn, template_id):
    rows = conn.execute(
        "SELECT * FROM recurring_bill_template_item WHERE template_id = ?",
        (template_id,)).fetchall()
    return [dict(r) for r in rows]


def _audit_count(conn, action=None):
    if action is None:
        row = conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE action = ?",
            (action,)).fetchone()
    return row["c"]


def _activate(conn, template_id, status="active"):
    return call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id=template_id, template_status=status,
    ))


# --------------------------------------------------------------------------
# activate then generate
# --------------------------------------------------------------------------

def test_activate_then_generate(conn, env):
    template_id = _add_monthly_template(conn, env)
    assert _read_template(conn, template_id)["status"] == "draft"

    result = _activate(conn, template_id)
    assert is_ok(result)
    assert result["template_id"] == template_id
    assert result["updated_fields"] == ["status"]
    assert _read_template(conn, template_id)["status"] == "active"

    result = call_action(mod.generate_recurring_bills, conn, _common_ns(
        company_id=env["company_id"], as_of_date="2026-09-26",
    ))
    assert is_ok(result)
    assert result["bills_generated"] == 1
    assert len(result["bills"]) == 1
    bill = result["bills"][0]
    assert bill["template_id"] == template_id
    assert bill["amount"] == "1000.00"
    pi = conn.execute(
        "SELECT * FROM purchase_invoice WHERE id = ?",
        (bill["invoice_id"],)).fetchone()
    assert pi is not None
    assert dict(pi)["posting_date"] == "2026-09-01"
    assert Decimal(bill["amount"]) == Decimal("1000.00")


def test_draft_template_generates_nothing(conn, env):
    _add_monthly_template(conn, env)
    result = call_action(mod.generate_recurring_bills, conn, _common_ns(
        company_id=env["company_id"], as_of_date="2026-09-26",
    ))
    assert is_ok(result)
    assert result["bills_generated"] == 0
    assert result["templates_processed"] == 0


def test_pause_stops_generation(conn, env):
    template_id = _add_monthly_template(conn, env)
    assert is_ok(_activate(conn, template_id))
    assert is_ok(_activate(conn, template_id, status="paused"))
    assert _read_template(conn, template_id)["status"] == "paused"
    result = call_action(mod.generate_recurring_bills, conn, _common_ns(
        company_id=env["company_id"], as_of_date="2026-09-26",
    ))
    assert is_ok(result)
    assert result["bills_generated"] == 0


# --------------------------------------------------------------------------
# refusals: exact message, rows unchanged, no audit row
# --------------------------------------------------------------------------

def test_unknown_template_id_refused(conn, env):
    before = _audit_count(conn)
    result = call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id="no-such-template", template_status="active",
    ))
    assert is_error(result)
    assert result["message"] == "Recurring bill template no-such-template not found"
    assert _read_template(conn, "no-such-template") is None
    assert _audit_count(conn) == before


def test_bad_status_refused(conn, env):
    template_id = _add_monthly_template(conn, env)
    before_tpl = _read_template(conn, template_id)
    before_items = _read_items(conn, template_id)
    before = _audit_count(conn)
    result = call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id=template_id, template_status="on",
    ))
    assert is_error(result)
    assert result["message"] == "--template-status must be 'active', 'paused', or 'cancelled'"
    assert _read_template(conn, template_id) == before_tpl
    assert _read_items(conn, template_id) == before_items
    assert _audit_count(conn) == before


def test_no_fields_refused(conn, env):
    template_id = _add_monthly_template(conn, env)
    before_tpl = _read_template(conn, template_id)
    before_items = _read_items(conn, template_id)
    before = _audit_count(conn)
    result = call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id=template_id,
    ))
    assert is_error(result)
    assert result["message"] == "No fields to update"
    assert _read_template(conn, template_id) == before_tpl
    assert _read_items(conn, template_id) == before_items
    assert _audit_count(conn) == before


def test_cancelled_template_cannot_change(conn, env):
    template_id = _add_monthly_template(conn, env)
    assert is_ok(_activate(conn, template_id, status="cancelled"))
    before_tpl = _read_template(conn, template_id)
    assert before_tpl["status"] == "cancelled"
    before_items = _read_items(conn, template_id)
    before = _audit_count(conn)
    result = call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id=template_id, frequency="weekly",
    ))
    assert is_error(result)
    assert result["message"] == (
        f"Recurring bill template {template_id} is 'cancelled' and cannot be changed")
    assert _read_template(conn, template_id) == before_tpl
    assert _read_items(conn, template_id) == before_items
    assert _audit_count(conn) == before


def test_bad_status_leaves_valid_frequency_untouched(conn, env):
    """Refusal order: frequency is validated-but-not-written before the
    status refusal, so a valid --frequency together with a bad
    --template-status changes nothing (read back on the same connection
    before any rollback)."""
    template_id = _add_monthly_template(conn, env)
    before = _audit_count(conn)
    result = call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id=template_id, frequency="weekly", template_status="on",
    ))
    assert is_error(result)
    assert result["message"] == "--template-status must be 'active', 'paused', or 'cancelled'"
    assert _read_template(conn, template_id)["frequency"] == "monthly"
    assert _read_template(conn, template_id)["status"] == "draft"
    assert _audit_count(conn) == before


def test_bad_items_leave_frequency_untouched(conn, env):
    """Refusal order for --items: a bad items line refuses before any write,
    so the valid --frequency in the same call is not applied either."""
    template_id = _add_monthly_template(conn, env)
    before_items = _read_items(conn, template_id)
    before = _audit_count(conn)
    result = call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id=template_id, frequency="weekly",
        items=json.dumps([{"qty": "2", "rate": "5.00"}]),
    ))
    assert is_error(result)
    assert result["message"] == "Item 0: item_id is required"
    assert _read_template(conn, template_id)["frequency"] == "monthly"
    assert _read_items(conn, template_id) == before_items
    assert _audit_count(conn) == before


# --------------------------------------------------------------------------
# audit + items replace
# --------------------------------------------------------------------------

def test_update_audit_row(conn, env):
    template_id = _add_monthly_template(conn, env)
    assert is_ok(_activate(conn, template_id))
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE action = ?",
        ("update-recurring-bill-template",)).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["entity_id"] == template_id
    assert row["entity_type"] == "recurring_bill_template"
    assert json.loads(row["old_values"])["status"] == "draft"
    new_values = json.loads(row["new_values"])
    assert new_values["status"] == "active"
    assert new_values["updated_fields"] == ["status"]


def test_replace_items(conn, env):
    template_id = _add_monthly_template(conn, env)
    new_items = json.dumps([
        {"item_id": env["item1"], "qty": "2", "rate": "500.00"},
        {"item_id": env["item2"], "qty": "3", "rate": "100.00"},
    ])
    result = call_action(mod.update_recurring_bill_template, conn, _common_ns(
        template_id=template_id, items=new_items,
    ))
    assert is_ok(result)
    assert result["updated_fields"] == ["items"]
    rows = _read_items(conn, template_id)
    assert len(rows) == 2
    by_item = {r["item_id"]: r for r in rows}
    assert set(by_item) == {env["item1"], env["item2"]}
    assert by_item[env["item1"]]["quantity"] == "2.00"
    assert by_item[env["item1"]]["rate"] == "500.00"
    assert by_item[env["item1"]]["amount"] == "1000.00"
    assert by_item[env["item2"]]["quantity"] == "3.00"
    assert by_item[env["item2"]]["rate"] == "100.00"
    assert by_item[env["item2"]]["amount"] == "300.00"
