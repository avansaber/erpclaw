"""Recurring bills auto-submit posts the same ledger legs as a hand submit.

A taxed template used to post its own two-leg entry (expense / payable, no
tax leg) instead of going through the submit path: the entry did not balance,
``insert_gl_entries`` refused it, and the template landed in ``errors``. The
auto-submit now goes through ``_submit_purchase_invoice_in_txn``, so a taxed
template auto-posts with its tax leg, one transaction per template.
"""
import json
import os
import sys
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from buying_helpers import (  # noqa: E402
    call_action, is_ok, load_db_query, ns,
)
from erpclaw_lib.query import Field, P, Q, Table, insert_row  # noqa: E402

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


def _u():
    return str(uuid.uuid4())


def _insert(conn, table, row):
    sql, _cols = insert_row(table, {key: P() for key in row})
    conn.execute(sql, tuple(row.values()))
    conn.commit()


def _tax_template(conn, env, rate, account_name="Input Tax"):
    tax_account = _u()
    _insert(conn, "account", {
        "id": tax_account, "name": account_name,
        "account_number": f"1400-{tax_account[:6]}", "root_type": "asset",
        "account_type": "tax", "balance_direction": "debit_normal",
        "company_id": env["company_id"], "depth": 0})
    tpl = _u()
    _insert(conn, "tax_template", {
        "id": tpl, "name": f"Purchase Tax {rate}-{tpl[:4]}",
        "tax_type": "purchase", "company_id": env["company_id"]})
    _insert(conn, "tax_template_line", {
        "id": _u(), "tax_template_id": tpl, "tax_account_id": tax_account,
        "rate": rate, "charge_type": "on_net_total", "row_order": 0,
        "add_deduct": "add"})
    return tpl, tax_account


def _activate(conn, template_id):
    t = Table("recurring_bill_template")
    q = (Q.update(t)
         .set(t.status, P())
         .where(t.id == P()))
    conn.execute(q.get_sql(), ("active", template_id))
    conn.commit()


def _add_template(conn, env, items, start_date, tax_template_id=None):
    result = call_action(mod.add_recurring_bill_template, conn, _common_ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        items=items, frequency="monthly",
        start_date=start_date, end_date="2026-12-31",
        tax_template_id=tax_template_id, auto_submit=True,
    ))
    assert is_ok(result), result
    _activate(conn, result["template_id"])
    return result["template_id"]


def _generate(conn, env, as_of_date):
    result = call_action(mod.generate_recurring_bills, conn, _common_ns(
        company_id=env["company_id"], as_of_date=as_of_date,
    ))
    assert is_ok(result), result
    return result


def _row(conn, table, row_id):
    t = Table(table)
    q = Q.from_(t).select(t.star).where(t.id == P())
    found = conn.execute(q.get_sql(), (row_id,)).fetchone()
    assert found is not None, f"{table} {row_id} not found"
    return dict(found)


def _all(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _gl_legs(conn, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(t.account_id, t.debit, t.credit, t.cost_center_id,
                 t.party_type, t.party_id, t.posting_date, t.is_cancelled,
                 t.remarks)
         .where(t.voucher_type == P())
         .where(t.voucher_id == P()))
    return [dict(r) for r in
            conn.execute(q.get_sql(), ("purchase_invoice", voucher_id)).fetchall()]


def _ple_rows(conn, voucher_id):
    t = Table("payment_ledger_entry")
    q = (Q.from_(t)
         .select(t.star)
         .where(t.voucher_type == P())
         .where(t.voucher_id == P()))
    return [dict(r) for r in
            conn.execute(q.get_sql(), ("purchase_invoice", voucher_id)).fetchall()]


def _audit_rows(conn, action, entity_id):
    t = Table("audit_log")
    q = (Q.from_(t).select(t.star)
         .where(t.action == P())
         .where(t.entity_id == P()))
    return [dict(r) for r in
            conn.execute(q.get_sql(), (action, entity_id)).fetchall()]


class TestTaxedTemplateAutoPosts:
    def test_taxed_template_posts_tax_leg(self, conn, env):
        tax_tpl, tax_account = _tax_template(conn, env, "10")
        template_id = _add_template(
            conn, env, _items(env, ("item1", "1", "100.00")),
            "2026-03-01", tax_template_id=tax_tpl)

        result = _generate(conn, env, "2026-03-15")

        assert result["bills_generated"] == 1
        assert result["errors"] == []
        bill = result["bills"][0]
        assert bill["status"] == "submitted"
        assert bill["amount"] == "110.00"
        assert bill["naming_series"] is not None
        pi_id = bill["invoice_id"]

        header = _row(conn, "purchase_invoice", pi_id)
        assert (header["total_amount"], header["tax_amount"],
                header["grand_total"], header["outstanding_amount"]) == (
            "100.00", "10.00", "110.00", "110.00")
        assert header["status"] == "submitted"

        legs = _gl_legs(conn, pi_id)
        assert len(legs) == 3
        assert all(leg["posting_date"] == "2026-03-01" for leg in legs)
        assert all(leg["is_cancelled"] == 0 for leg in legs)
        assert sorted(
            (leg["account_id"], leg["debit"], leg["credit"]) for leg in legs
        ) == sorted([
            (env["expense"], "100.00", "0.00"),
            (tax_account, "10.00", "0.00"),
            (env["ap"], "0.00", "110.00"),
        ])
        by_account = {leg["account_id"]: leg for leg in legs}
        assert by_account[env["expense"]]["cost_center_id"] == env["cc"]
        assert by_account[env["ap"]]["party_type"] == "supplier"
        assert by_account[env["ap"]]["party_id"] == env["supplier"]

        ple = _ple_rows(conn, pi_id)
        assert len(ple) == 1
        assert ple[0]["amount"] == "110.00"
        assert ple[0]["delinked"] == 0

        audit = _audit_rows(conn, "submit-purchase-invoice", pi_id)
        assert len(audit) == 1

        template = _row(conn, "recurring_bill_template", template_id)
        assert template["last_bill_date"] == "2026-03-01"
        assert template["next_bill_date"] == "2026-04-01"

    def test_two_lines_rounded_tax_posts_one_leg_per_line(self, conn, env):
        tax_tpl, tax_account = _tax_template(conn, env, "7.5")
        _add_template(
            conn, env,
            _items(env, ("item1", "2", "45.50"), ("item2", "1", "21.33")),
            "2026-04-01", tax_template_id=tax_tpl)

        result = _generate(conn, env, "2026-04-10")

        assert result["bills_generated"] == 1
        assert result["errors"] == []
        bill = result["bills"][0]
        assert bill["status"] == "submitted"
        pi_id = bill["invoice_id"]

        header = _row(conn, "purchase_invoice", pi_id)
        assert (header["total_amount"], header["tax_amount"],
                header["grand_total"]) == ("112.33", "8.42", "120.75")

        legs = _gl_legs(conn, pi_id)
        assert len(legs) == 4
        assert sorted(
            (leg["account_id"], leg["debit"], leg["credit"]) for leg in legs
        ) == sorted([
            (env["expense"], "91.00", "0.00"),
            (env["expense"], "21.33", "0.00"),
            (tax_account, "8.42", "0.00"),
            (env["ap"], "0.00", "120.75"),
        ])

        ple = _ple_rows(conn, pi_id)
        assert len(ple) == 1
        assert ple[0]["amount"] == "120.75"


class TestUntaxedControl:
    def test_untaxed_template_posts_exactly_as_before(self, conn, env):
        template_id = _add_template(
            conn, env, _items(env, ("item1", "1", "500.00")), "2026-02-01")

        result = _generate(conn, env, "2026-02-10")

        assert result["bills_generated"] == 1
        assert result["errors"] == []
        bill = result["bills"][0]
        assert bill["status"] == "submitted"
        pi_id = bill["invoice_id"]
        expected_remarks = f"Recurring Bill from template {template_id}"

        legs = _gl_legs(conn, pi_id)
        assert len(legs) == 2
        assert sorted(
            (leg["account_id"], leg["debit"], leg["credit"]) for leg in legs
        ) == sorted([
            (env["expense"], "500.00", "0.00"),
            (env["ap"], "0.00", "500.00"),
        ])
        by_account = {leg["account_id"]: leg for leg in legs}
        assert by_account[env["expense"]]["cost_center_id"] == env["cc"]
        assert by_account[env["ap"]]["party_type"] == "supplier"
        assert by_account[env["ap"]]["party_id"] == env["supplier"]
        assert all(leg["remarks"] == expected_remarks for leg in legs)

        ple = _ple_rows(conn, pi_id)
        assert len(ple) == 1
        assert ple[0]["amount"] == "500.00"
        assert ple[0]["remarks"] == expected_remarks


class TestFailingTemplateIsolation:
    def test_failing_template_writes_nothing_and_spares_the_other(
            self, conn, env):
        disabled_account = _u()
        _insert(conn, "account", {
            "id": disabled_account, "name": "Input Tax Disabled",
            "account_number": f"1400-{disabled_account[:6]}",
            "root_type": "asset", "account_type": "tax",
            "balance_direction": "debit_normal",
            "company_id": env["company_id"], "depth": 0, "disabled": 1})
        tax_tpl = _u()
        _insert(conn, "tax_template", {
            "id": tax_tpl, "name": f"Purchase Tax 10-{tax_tpl[:4]}",
            "tax_type": "purchase", "company_id": env["company_id"]})
        _insert(conn, "tax_template_line", {
            "id": _u(), "tax_template_id": tax_tpl,
            "tax_account_id": disabled_account,
            "rate": "10", "charge_type": "on_net_total", "row_order": 0,
            "add_deduct": "add"})
        template_a = _add_template(
            conn, env, _items(env, ("item1", "1", "100.00")),
            "2026-05-01", tax_template_id=tax_tpl)
        template_b = _add_template(
            conn, env, _items(env, ("item2", "1", "200.00")), "2026-05-01")

        result = _generate(conn, env, "2026-05-15")

        assert result["templates_processed"] == 2
        assert result["bills_generated"] == 1
        assert len(result["bills"]) == 1
        bill = result["bills"][0]
        assert bill["template_id"] == template_b
        assert bill["amount"] == "200.00"
        assert result["errors"] == [{
            "template_id": template_a,
            "error": ("GL posting failed: GL Validation Step 2 Failed: "
                      "Account 'Input Tax Disabled' is disabled"),
        }]
        bill_b = bill["invoice_id"]

        invoices = _all(conn, "purchase_invoice")
        assert len(invoices) == 1
        assert invoices[0]["id"] == bill_b
        lines = _all(conn, "purchase_invoice_item")
        assert len(lines) == 1
        assert lines[0]["purchase_invoice_id"] == bill_b

        all_gl = _all(conn, "gl_entry")
        assert len(all_gl) == 2
        assert {row["voucher_id"] for row in all_gl} == {bill_b}
        all_ple = _all(conn, "payment_ledger_entry")
        assert len(all_ple) == 1
        assert all_ple[0]["voucher_id"] == bill_b

        audit_t = Table("audit_log")
        audit_q = (Q.from_(audit_t).select(audit_t.star)
                   .where(audit_t.entity_type == P()))
        purchase_audits = [dict(r) for r in conn.execute(
            audit_q.get_sql(), ("purchase_invoice",)).fetchall()]
        assert len(purchase_audits) == 1
        assert purchase_audits[0]["entity_id"] == bill_b

        template_a_row = _row(conn, "recurring_bill_template", template_a)
        assert template_a_row["next_bill_date"] == "2026-05-01"
        assert template_a_row["last_bill_date"] is None
        assert template_a_row["status"] == "active"
        template_b_row = _row(conn, "recurring_bill_template", template_b)
        assert template_b_row["next_bill_date"] == "2026-06-01"
