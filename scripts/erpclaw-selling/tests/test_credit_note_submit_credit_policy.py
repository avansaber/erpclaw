"""Submitting a credit note is never refused by the credit policy (m720).

A credit note (sales_invoice with is_return=1, negative grand_total) only
reduces what the customer owes, so submit_sales_invoice must skip the whole
credit policy (suspended/on_hold status checks and the credit-limit check)
for it. Ordinary invoices are unchanged.
"""
import json
from decimal import Decimal

import pytest

from selling_helpers import call_action, ns, is_error, is_ok, load_db_query
from test_inv25_flows import _create_invoice

mod = load_db_query()


def _set_credit_limit(conn, customer_id, limit):
    conn.execute(
        "UPDATE customer SET credit_limit=? WHERE id=?",
        (limit, customer_id),
    )


def _set_credit_status(conn, customer_id, credit_status):
    conn.execute(
        "UPDATE customer SET credit_status=? WHERE id=?",
        (credit_status, customer_id),
    )


def _submit(conn, sales_invoice_id):
    return call_action(
        mod.submit_sales_invoice, conn, ns(sales_invoice_id=sales_invoice_id))


def _create_credit_note(conn, env, against_invoice_id, qty, rate):
    result = call_action(mod.create_credit_note, conn, ns(
        against_invoice_id=against_invoice_id,
        reason="Returned goods",
        posting_date="2026-06-25",
        items=json.dumps([{
            "item_id": env["item1"], "qty": qty, "rate": rate}]),
    ))
    assert is_ok(result), result
    return result["credit_note_id"]


def _invoice_row(conn, sales_invoice_id):
    return conn.execute(
        "SELECT is_return, grand_total, status FROM sales_invoice WHERE id=?",
        (sales_invoice_id,)).fetchone()


def _setup_900_less_500(conn, env):
    """Limit 1000.00, 9x100.00 invoice submitted, 5x100.00 note submitted."""
    _set_credit_limit(conn, env["customer"], "1000.00")
    si_id = _create_invoice(conn, env, qty="9", rate="100.00")
    result = _submit(conn, si_id)
    assert is_ok(result), result
    cn_id = _create_credit_note(conn, env, si_id, "5", "100.00")
    result = _submit(conn, cn_id)
    assert is_ok(result), result
    return si_id, cn_id


def test_credit_note_submits_under_limit(conn, env):
    _set_credit_limit(conn, env["customer"], "1000.00")
    si_id = _create_invoice(conn, env, qty="9", rate="100.00")
    assert is_ok(_submit(conn, si_id))

    cn_id = _create_credit_note(conn, env, si_id, "5", "100.00")
    result = _submit(conn, cn_id)
    assert is_ok(result), result

    row = _invoice_row(conn, cn_id)
    assert row["is_return"] == 1
    assert Decimal(row["grand_total"]) == Decimal("-500.00")
    assert row["status"] == "submitted"

    result = call_action(mod.check_credit_limit, conn, ns(
        customer_id=env["customer"]))
    assert is_ok(result)
    assert Decimal(result["outstanding_ar"]) == Decimal("400.00")
    assert Decimal(result["available_credit"]) == Decimal("600.00")


def test_new_sale_fits_after_note(conn, env):
    _setup_900_less_500(conn, env)
    si_id = _create_invoice(conn, env, qty="5", rate="100.00")
    result = _submit(conn, si_id)
    assert is_ok(result), result


def test_over_limit_still_refused(conn, env):
    _setup_900_less_500(conn, env)
    si_id = _create_invoice(conn, env, qty="5", rate="100.00")
    assert is_ok(_submit(conn, si_id))

    si_id = _create_invoice(conn, env, qty="2", rate="100.00")
    result = _submit(conn, si_id)
    assert is_error(result)
    assert result["message"].startswith(
        "Credit limit exceeded: outstanding=900.00 + new=200.00")
    row = conn.execute(
        "SELECT status FROM sales_invoice WHERE id=?", (si_id,)).fetchone()
    assert row["status"] == "draft"


@pytest.mark.parametrize("credit_status", ["on_hold", "suspended"])
def test_credit_note_submits_while_restricted(conn, env, credit_status):
    si_id = _create_invoice(conn, env, qty="3", rate="100.00")
    assert is_ok(_submit(conn, si_id))

    _set_credit_status(conn, env["customer"], credit_status)

    cn_id = _create_credit_note(conn, env, si_id, "1", "100.00")
    result = _submit(conn, cn_id)
    assert is_ok(result), result

    si_id = _create_invoice(conn, env, qty="1", rate="100.00")
    result = _submit(conn, si_id)
    assert is_error(result)
    if credit_status == "on_hold":
        assert result["message"].startswith(
            "Customer credit is on hold; cannot submit new invoice")
    else:
        assert result["message"].startswith(
            "Customer credit is suspended; cannot submit new invoice")


def test_credit_note_submits_with_no_limit(conn, env):
    _set_credit_limit(conn, env["customer"], "0")
    si_id = _create_invoice(conn, env, qty="3", rate="100.00")
    assert is_ok(_submit(conn, si_id))

    cn_id = _create_credit_note(conn, env, si_id, "1", "100.00")
    result = _submit(conn, cn_id)
    assert is_ok(result), result
