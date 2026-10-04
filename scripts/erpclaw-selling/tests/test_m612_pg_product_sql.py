"""Product SQL fixes for PostgreSQL parity plus exact-decimal packing.

Covers five defects that share one root pattern (SQLite-only SQL or a
SQLite-only exception type in product code):

  - credit exposure (``_customer_outstanding_ar``): exact-decimal sum with a
    text fallback, so ``check_credit_limit`` and the submit-time credit
    policy read the same outstanding on both backends;
  - packing (``add_packing_slip``): the already-packed total uses the
    exact-decimal sum helper and ``Decimal`` comparison, so repeated small
    quantities never drift through binary float;
  - duplicate refusals (``add_dunning_level``, ``add_customer``,
    ``add_sales_partner``): unique violations surface as the clean error
    JSON on both backends via the shared integrity-error lookup.

Each test runs on SQLite always, and on PostgreSQL when the suite's
database fixture is pointed at the test URL.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)
from erpclaw_lib.query import Q, P, Table, fn

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))  # scripts/


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pay = _load("db_query_payments_m612", "erpclaw-payments/db_query.py")


def _receive_payment(conn, env, amount, posting_date, allocations):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date=posting_date, party_type="customer",
        party_id=env["customer"],
        paid_from_account=env["ar"], paid_to_account=env["cash"],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations),
        deductions=None,
    ))
    assert is_ok(created), created
    submitted = call_action(pay.submit_payment, conn, ns(
        payment_entry_id=created["payment_entry_id"],
    ))
    assert is_ok(submitted), submitted
    return created["payment_entry_id"]


def _invoice_status_outstanding(conn, invoice_id):
    row = conn.execute(
        Q.from_(_t_sales_invoice)
         .select(_t_sales_invoice.status, _t_sales_invoice.outstanding_amount)
         .where(_t_sales_invoice.id == P())
         .get_sql(),
        (invoice_id,),
    ).fetchone()
    return (row[0], row[1])

_t_customer = Table("customer")
_t_sales_invoice = Table("sales_invoice")
_t_gl_entry = Table("gl_entry")
_t_payment_ledger_entry = Table("payment_ledger_entry")
_t_stock_ledger_entry = Table("stock_ledger_entry")
_t_delivery_note = Table("delivery_note")
_t_delivery_note_item = Table("delivery_note_item")
_t_packing_slip = Table("packing_slip")
_t_packing_slip_item = Table("packing_slip_item")
_t_dunning_level = Table("dunning_level")
_t_sales_partner = Table("sales_partner")


def _table_count(conn, table):
    """Row count for one table (no filter, so no parameters to bind)."""
    tbl = Table(table)
    return conn.execute(
        Q.from_(tbl).select(fn.Count("*")).get_sql(),
    ).fetchone()[0]


def _snapshot_tables(conn, tables):
    """Whole-row snapshots (every row, ordered by id, as dicts)."""
    snap = {}
    for name in tables:
        tbl = Table(name)
        rows = conn.execute(
            Q.from_(tbl).select(tbl.star).orderby(tbl.id).get_sql(),
        ).fetchall()
        snap[name] = [dict(r) for r in rows]
    return snap


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_and_submit_invoice(conn, env, qty, rate):
    """Create a standalone invoice and submit it; return its id."""
    create = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, ("item1", qty, rate)),
        tax_template_id=None, payment_terms_id=None,
    ))
    assert is_ok(create), create
    sub = call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=create["sales_invoice_id"],
    ))
    assert is_ok(sub), sub
    return create["sales_invoice_id"]


def _credit_scene(conn, env):
    """Two submitted invoices (500 + 300), one submitted credit note (-200),
    credit limit 1000. Outstanding exposure is 600.00."""
    conn.execute(
        Q.update(_t_customer)
         .set(_t_customer.credit_limit, P())
         .where(_t_customer.id == P())
         .get_sql(),
        ("1000.00", env["customer"]),
    )
    conn.commit()
    inv_a = _create_and_submit_invoice(conn, env, "5", "100.00")
    _create_and_submit_invoice(conn, env, "3", "100.00")
    cn = call_action(mod.create_credit_note, conn, ns(
        against_invoice_id=inv_a,
        reason="Returned goods",
        posting_date="2026-06-25",
        items=json.dumps([{"item_id": env["item1"], "qty": "2", "rate": "100.00"}]),
    ))
    assert is_ok(cn), cn
    cn_sub = call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=cn["credit_note_id"],
    ))
    assert is_ok(cn_sub), cn_sub
    return inv_a


# ──────────────────────────────────────────────────────────────────────────────
# Credit exposure
# ──────────────────────────────────────────────────────────────────────────────

class TestCreditExposureExact:
    def test_outstanding_with_invoices_and_credit_note(self, conn, env):
        _credit_scene(conn, env)
        result = call_action(mod.check_credit_limit, conn, ns(
            customer_id=env["customer"],
        ))
        assert is_ok(result), result
        assert result["outstanding_ar"] == "600.00"
        assert result["credit_limit"] == "1000.00"
        assert result["available_credit"] == "400.00"
        assert result["limit_enforced"] is True

    def test_credit_note_subtracted(self, conn, env):
        _credit_scene(conn, env)
        result = call_action(mod.check_credit_limit, conn, ns(
            customer_id=env["customer"],
        ))
        assert is_ok(result), result
        assert result["outstanding_ar"] == "600.00"
        assert result["available_credit"] == "400.00"

    def test_submit_over_limit_refused_database_unchanged(self, conn, env):
        _credit_scene(conn, env)
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=_items(env, ("item1", "5", "100.00")),
            tax_template_id=None, payment_terms_id=None,
        ))
        assert is_ok(create), create
        new_id = create["sales_invoice_id"]
        tables = ("sales_invoice", "gl_entry",
                  "payment_ledger_entry", "stock_ledger_entry")
        before = _snapshot_tables(conn, tables)
        refused = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=new_id,
        ))
        assert is_error(refused), refused
        assert refused["message"] == (
            "Credit limit exceeded: outstanding=600.00 + new=500.00 = 1100.00 "
            "> limit=1000.00. Raise credit limit, collect on outstanding "
            "invoices, or place customer on hold to acknowledge."
        )
        row = conn.execute(
            Q.from_(_t_sales_invoice)
             .select(_t_sales_invoice.status)
             .where(_t_sales_invoice.id == P())
             .get_sql(),
            (new_id,),
        ).fetchone()
        assert row[0] == "draft"
        assert _snapshot_tables(conn, tables) == before

    def test_partially_paid_invoice_counts_in_exposure(self, conn, env):
        conn.execute(
            Q.update(_t_customer)
             .set(_t_customer.credit_limit, P())
             .where(_t_customer.id == P())
             .get_sql(),
            ("1000.00", env["customer"]),
        )
        conn.commit()
        inv_big = _create_and_submit_invoice(conn, env, "5", "100.00")
        _create_and_submit_invoice(conn, env, "3", "100.00")
        _receive_payment(conn, env, "200.00", "2026-06-25", [
            {"voucher_type": "sales_invoice", "voucher_id": inv_big,
             "allocated_amount": "200.00"},
        ])
        assert _invoice_status_outstanding(conn, inv_big) == (
            "partially_paid", "300.00")
        result = call_action(mod.check_credit_limit, conn, ns(
            customer_id=env["customer"],
        ))
        assert is_ok(result), result
        assert result["outstanding_ar"] == "600.00"
        assert result["available_credit"] == "400.00"

    def test_full_credit_note_clears_exposure(self, conn, env):
        inv = _create_and_submit_invoice(conn, env, "3", "100.00")
        cn = call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=inv,
            reason="Full return",
            posting_date="2026-06-25",
            items=json.dumps(
                [{"item_id": env["item1"], "qty": "3", "rate": "100.00"}]),
        ))
        assert is_ok(cn), cn
        cn_sub = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=cn["credit_note_id"],
        ))
        assert is_ok(cn_sub), cn_sub
        assert _invoice_status_outstanding(conn, inv) == (
            "paid", "0")
        assert _invoice_status_outstanding(conn, cn["credit_note_id"]) == (
            "submitted", "0")
        result = call_action(mod.check_credit_limit, conn, ns(
            customer_id=env["customer"],
        ))
        assert is_ok(result), result
        assert result["outstanding_ar"] == "0"


# ──────────────────────────────────────────────────────────────────────────────
# Packing with exact decimals
# ──────────────────────────────────────────────────────────────────────────────

def _seed_dn(conn, env, qty="0.3"):
    """Insert a draft delivery note with one line of ``qty`` units."""
    dn_id = str(uuid.uuid4())
    dni_id = str(uuid.uuid4())
    conn.execute(
        Q.into(_t_delivery_note)
         .columns("id", "customer_id", "posting_date", "status",
                  "total_qty", "company_id")
         .insert(P(), P(), P(), P(), P(), P())
         .get_sql(),
        (dn_id, env["customer"], "2026-06-01", "draft", qty,
         env["company_id"]),
    )
    amount = str(Decimal(qty) * Decimal("50.00"))
    conn.execute(
        Q.into(_t_delivery_note_item)
         .columns("id", "delivery_note_id", "item_id", "quantity",
                  "uom", "rate", "amount")
         .insert(P(), P(), P(), P(), P(), P(), P())
         .get_sql(),
        (dni_id, dn_id, env["item1"], qty, "Each", "50.00", amount),
    )
    conn.commit()
    return dn_id, dni_id


def _pack(conn, env, dn_id, dni_id, qty_packed):
    return call_action(mod.add_packing_slip, conn, ns(
        delivery_note_id=dn_id,
        items=json.dumps([{
            "delivery_note_item_id": dni_id, "qty_packed": qty_packed,
        }]),
        posting_date="2026-06-15",
        notes=None, reason=None,
        company_id=env["company_id"],
    ))


def _stored_packed_qtys(conn, dni_id):
    rows = conn.execute(
        Q.from_(_t_packing_slip_item)
         .select(_t_packing_slip_item.qty_packed)
         .where(_t_packing_slip_item.delivery_note_item_id == P())
         .get_sql(),
        (dni_id,),
    ).fetchall()
    return sorted(r[0] for r in rows)


class TestPackingExactDecimal:
    def test_three_tenths_fill_three_tenths_fourth_refused(self, conn, env):
        dn_id, dni_id = _seed_dn(conn, env, "0.3")
        for _ in range(3):
            result = _pack(conn, env, dn_id, dni_id, "0.1")
            assert is_ok(result), result
        assert _stored_packed_qtys(conn, dni_id) == ["0.10", "0.10", "0.10"]
        before_slips = _table_count(conn, "packing_slip")
        fourth = _pack(conn, env, dn_id, dni_id, "0.1")
        assert is_error(fourth), fourth
        assert fourth["message"] == (
            "Item 0: packed qty 0.1 + already packed 0.30 exceeds DN qty 0.3"
        )
        assert _table_count(conn, "packing_slip") == before_slips
        assert _stored_packed_qtys(conn, dni_id) == ["0.10", "0.10", "0.10"]

    def test_tenths_split_fills_seven_tenths_exact(self, conn, env):
        dn_id, dni_id = _seed_dn(conn, env, "0.7")
        assert is_ok(_pack(conn, env, dn_id, dni_id, "0.1"))
        assert is_ok(_pack(conn, env, dn_id, dni_id, "0.2"))
        assert _stored_packed_qtys(conn, dni_id) == ["0.10", "0.20"]
        third = _pack(conn, env, dn_id, dni_id, "0.4")
        assert is_ok(third), third
        assert _stored_packed_qtys(conn, dni_id) == ["0.10", "0.20", "0.40"]


# ──────────────────────────────────────────────────────────────────────────────
# Duplicate refusals (unique violations are clean errors on both backends)
# ──────────────────────────────────────────────────────────────────────────────

_FIXED_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")


class _FixedUUID:
    """Stand-in for the ``uuid`` module: every new id collides.

    Patching the selling module's ``uuid`` attribute (not the shared stdlib
    module) pins only the id the action under test mints, so the second
    call hits the table's uniqueness rule through the normal path.
    """

    @staticmethod
    def uuid4():
        return _FIXED_UUID


def _customer_args(env, name="Dup Customer"):
    return dict(
        name=name, company_id=env["company_id"],
        customer_type=None, customer_group=None,
        payment_terms_id=None, credit_limit=None,
        tax_id=None, exempt_from_sales_tax=None,
        primary_address=None, primary_contact=None,
    )


class TestDuplicateRefusals:
    def test_duplicate_dunning_level(self, conn, env):
        first = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=2, days_overdue=60,
            dunning_action="hold",
            template_id=None, description=None,
        ))
        assert is_ok(first), first
        before = _table_count(conn, "dunning_level")
        second = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=2, days_overdue=90,
            dunning_action="call",
            template_id=None, description=None,
        ))
        assert is_error(second), second
        assert "already exists" in second["message"]
        conn.rollback()
        assert _table_count(conn, "dunning_level") == before
        row = conn.execute(
            Q.from_(_t_dunning_level)
             .select(_t_dunning_level.days_overdue)
             .where(_t_dunning_level.id == P())
             .get_sql(),
            (first["id"],),
        ).fetchone()
        assert row[0] == 60

    def test_duplicate_customer(self, conn, env):
        with patch.object(mod, "uuid", _FixedUUID):
            first = call_action(
                mod.add_customer, conn, ns(**_customer_args(env)))
        assert is_ok(first), first
        before = _table_count(conn, "customer")
        with patch.object(mod, "uuid", _FixedUUID):
            second = call_action(
                mod.add_customer, conn, ns(**_customer_args(env)))
        assert is_error(second), second
        assert second["message"] == (
            "Customer creation failed \u2014 check for duplicates or invalid data"
        )
        conn.rollback()
        assert _table_count(conn, "customer") == before

    def test_duplicate_sales_partner(self, conn, env):
        with patch.object(mod, "uuid", _FixedUUID):
            first = call_action(mod.add_sales_partner, conn, ns(
                name="Dup Partner", commission_rate="10.00",
            ))
        assert is_ok(first), first
        before = _table_count(conn, "sales_partner")
        with patch.object(mod, "uuid", _FixedUUID):
            second = call_action(mod.add_sales_partner, conn, ns(
                name="Dup Partner", commission_rate="10.00",
            ))
        assert is_error(second), second
        assert second["message"] == (
            "Sales partner creation failed \u2014 check for duplicates or invalid data"
        )
        conn.rollback()
        assert _table_count(conn, "sales_partner") == before
