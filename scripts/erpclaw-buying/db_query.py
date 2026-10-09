#!/usr/bin/env python3
"""ERPClaw Buying Skill — db_query.py

Procure-to-pay cycle: suppliers, material requests, RFQs, supplier quotations,
purchase orders, purchase receipts (GRN), purchase invoices, debit notes,
landed cost vouchers.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import sqlite3
import sys
import uuid
import calendar
import re
import hashlib
import shutil
import stat
import struct
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta, date as date_type
from decimal import Decimal, InvalidOperation

# Add shared lib to path
try:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.db import get_connection, unexpected_error_message
    from erpclaw_lib.decimal_utils import to_decimal, round_currency
    from erpclaw_lib.validation import check_input_lengths
    from erpclaw_lib.naming import get_next_name
    from erpclaw_lib.stock_posting import (
        insert_sle_entries,
        reverse_sle_entries,
        get_stock_balance,
        get_valuation_rate,
        create_perpetual_inventory_gl,
        reprice_stock_valuation,
    )
    from erpclaw_lib.gl_posting import insert_gl_entries, reverse_gl_entries, take_chain_heads
    from erpclaw_lib.cwip_posting import (
        get_under_construction_asset, resolve_cwip_account, record_cwip_accumulation,
        reverse_cwip_accumulations,
    )
    from erpclaw_lib.dimensions import (
        parse_dimension_input,
        validate_document_dimensions,
        dimensions_json_text,
    )
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.custom_fields import store_from_arg, merge_into_response
    from erpclaw_lib.dependencies import check_required_tables
    from erpclaw_lib.query_helpers import get_default_cost_center, get_fiscal_year, resolve_company_id, resolve_scope_company
    from erpclaw_lib.query import Q, P, Table, Field, fn, Case, Order, Criterion, Not, NULL, DecimalSum, DecimalAbs, dynamic_update, line_order, scalar_max, now
    from erpclaw_lib import authority_gate, company_scope
    from erpclaw_lib.authorization_consumption import INPUT_INVALID
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args
    from erpclaw_lib.vendor.pypika.terms import LiteralValue, ValueWrapper
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw first: clawhub install erpclaw", "suggestion": "clawhub install erpclaw"}))
    sys.exit(1)



REQUIRED_TABLES = ["company", "account", "item"]

VALID_FREQUENCIES = ("weekly", "monthly", "quarterly", "semi_annually", "annually")


def _dimension_input(args):
    try:
        return parse_dimension_input(
            getattr(args, "dimensions", None),
            getattr(args, "dimension_key", None),
            getattr(args, "dimension_value", None))
    except ValueError as e:
        err(str(e))


def _validate_dims_before_write(conn, obj):
    if obj:
        try:
            validate_document_dimensions(conn, obj)
        except ValueError as e:
            err(str(e))

# ---------------------------------------------------------------------------
# PyPika table references (for new features)
# ---------------------------------------------------------------------------
_t_blanket_order = Table("blanket_order")
_t_blanket_order_item = Table("blanket_order_item")
_t_purchase_order = Table("purchase_order")
_t_purchase_order_item = Table("purchase_order_item")
_t_purchase_invoice = Table("purchase_invoice")
_t_purchase_invoice_item = Table("purchase_invoice_item")
_t_recurring_bill_template = Table("recurring_bill_template")
_t_recurring_bill_template_item = Table("recurring_bill_template_item")
_t_sales_order = Table("sales_order")
_t_sales_order_item = Table("sales_order_item")
_t_item_supplier = Table("item_supplier")
_t_company = Table("company")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_arg(value, name):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        err(f"Invalid JSON for --{name}: {value}")


def _get_cost_center(conn, company_id: str) -> str | None:
    return get_default_cost_center(conn, company_id)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _calculate_tax(conn, tax_template_id, subtotal):
    """Calculate tax from template. Returns (tax_amount, tax_details list)."""
    if not tax_template_id:
        return Decimal("0"), []
    ttl = Table("tax_template_line").as_("ttl")
    a = Table("account").as_("a")
    q = (Q.from_(ttl)
         .left_join(a).on(a.id == ttl.tax_account_id)
         .select(ttl.star, a.name.as_("account_name"))
         .where(ttl.tax_template_id == P())
         .orderby(ttl.row_order))
    lines = conn.execute(q.get_sql(), (tax_template_id,)).fetchall()
    if not lines:
        return Decimal("0"), []
    total_tax = Decimal("0")
    details = []
    cumulative = subtotal
    for line in lines:
        ld = row_to_dict(line)
        rate = to_decimal(ld.get("rate", "0"))
        charge_type = ld.get("charge_type", "on_net_total")
        if charge_type == "on_net_total":
            tax_amt = round_currency(subtotal * rate / Decimal("100"))
        elif charge_type == "on_previous_row_total":
            tax_amt = round_currency(cumulative * rate / Decimal("100"))
        elif charge_type == "actual":
            tax_amt = round_currency(rate)
        else:
            tax_amt = round_currency(subtotal * rate / Decimal("100"))
        if ld.get("add_deduct") == "deduct":
            tax_amt = -tax_amt
        total_tax += tax_amt
        cumulative += tax_amt
        details.append({
            "tax_account_id": ld["tax_account_id"],
            "account_name": ld.get("account_name"),
            "rate": str(rate),
            "tax_amount": str(tax_amt),
        })
    return round_currency(total_tax), details


def _order_line_net(label, amount, item):
    """Split a purchase-order line gross amount into discount parts.

    No database access. `amount` is the already rounded Decimal gross;
    `item` is the line dict, which may carry `discount_percentage` or
    `discount_amount` (money), never both. Returns
    (stored_pct, net_amount, discount), all rounded Decimals.
    """
    has_pct = "discount_percentage" in item and item["discount_percentage"] is not None
    has_amt = "discount_amount" in item and item["discount_amount"] is not None
    if has_pct and has_amt:
        err(f"Item {label}: give discount_percentage or discount_amount, not both")
    if has_amt:
        d = to_decimal(item["discount_amount"])
        if d < 0:
            err(f"Item {label}: discount_amount must not be negative")
        discount = round_currency(d)
        net_amount = amount - discount
        if amount == 0:
            stored_pct = round_currency(Decimal("0"))
        else:
            stored_pct = round_currency(discount / amount * Decimal("100"))
    else:
        raw_pct = item.get("discount_percentage", "0")
        if raw_pct is None:
            raw_pct = "0"
        pct = to_decimal(raw_pct)
        if pct < 0 or pct >= 100:
            err(f"Item {label}: discount_percentage must be at least 0 and less than 100")
        net_amount = round_currency(amount * (Decimal("1") - pct / Decimal("100")))
        stored_pct = round_currency(pct)
        discount = round_currency(amount - net_amount)
    if discount > 0 and discount >= amount:
        err(f"Item {label}: discount {discount} must be less than the line amount {amount}")
    return stored_pct, net_amount, discount


def discount_share(D, Q, prior_qty, prior_disc, q):
    """Share of an order line's discount carried by a receipt line.

    Pure function, no database access. All arguments are Decimals.
    `D` is the order line discount, `Q` its quantity, `prior_qty` and
    `prior_disc` the quantity and discount already covered by prior
    documents, and `q` this line's quantity. Covered units carry `D/Q`
    rounded to the cent; the line completing `Q` takes the remainder so
    covered lines sum to `D` exactly; excess units carry the per-unit share.
    """
    D = to_decimal(D)
    Q = to_decimal(Q)
    prior_qty = to_decimal(prior_qty)
    prior_disc = to_decimal(prior_disc)
    q = to_decimal(q)
    if Q == 0:
        return round_currency(Decimal("0"))
    covered = min(q, max(Q - prior_qty, Decimal("0")))
    excess = q - covered
    if covered > 0 and prior_qty + covered == Q:
        d_cov = D - prior_disc
    else:
        d_cov = round_currency(D * covered / Q)
    return round_currency(d_cov + round_currency(D * excess / Q))


def _discount_text(value):
    """Stored text for a discount share: str(value), zero as "0"."""
    if value == 0:
        return "0"
    return str(value)


def _receipt_prior_sums(conn, purchase_order_item_id):
    """Prior (qty, discount) over submitted receipt lines for one order line.

    PyPika with bound parameters; sums in Python Decimal, never in SQL.
    """
    pri_t = Table("purchase_receipt_item")
    pr_t = Table("purchase_receipt")
    q = (Q.from_(pri_t)
         .join(pr_t).on(pr_t.id == pri_t.purchase_receipt_id)
         .select(pri_t.quantity, pri_t.discount_amount)
         .where(pri_t.purchase_order_item_id == P())
         .where(pr_t.status == ValueWrapper("submitted")))
    rows = conn.execute(q.get_sql(), (purchase_order_item_id,)).fetchall()
    qty = Decimal("0")
    disc = Decimal("0")
    for r in rows:
        qty += to_decimal(r["quantity"])
        disc += to_decimal(r["discount_amount"])
    return qty, disc


def _preview_line_discount(conn, poi_row, qty, intra_qty, intra_disc):
    """Preview share for one receipt line, accumulating intra-document prior."""
    po_item_id = poi_row["id"]
    qty = to_decimal(qty)
    order_qty = to_decimal(poi_row["quantity"])
    order_disc = round_currency(
        to_decimal(poi_row["amount"]) - to_decimal(poi_row["net_amount"]))
    sub_qty, sub_disc = _receipt_prior_sums(conn, po_item_id)
    prior_qty = sub_qty + intra_qty.get(po_item_id, Decimal("0"))
    prior_disc = sub_disc + intra_disc.get(po_item_id, Decimal("0"))
    share = discount_share(order_disc, order_qty, prior_qty, prior_disc, qty)
    intra_qty[po_item_id] = intra_qty.get(po_item_id, Decimal("0")) + qty
    intra_disc[po_item_id] = intra_disc.get(po_item_id, Decimal("0")) + share
    return share


def _bill_prior_sums(conn, purchase_order_item_id):
    """Prior (qty, discount) over posted bill lines for one order line.

    PyPika with bound parameters; sums in Python Decimal, never in SQL.
    Counts only non-return invoices with a posted status.
    """
    pii_t = Table("purchase_invoice_item")
    pi_t = Table("purchase_invoice")
    q = (Q.from_(pii_t)
         .join(pi_t).on(pi_t.id == pii_t.purchase_invoice_id)
         .select(pii_t.quantity, pii_t.discount_amount)
         .where(pii_t.purchase_order_item_id == P())
         .where(pi_t.is_return == 0)
         .where(pi_t.status.isin([P(), P(), P(), P()])))
    rows = conn.execute(
        q.get_sql(),
        (purchase_order_item_id,
         "submitted", "partially_paid", "paid", "overdue")).fetchall()
    qty = Decimal("0")
    disc = Decimal("0")
    for r in rows:
        qty += to_decimal(r["quantity"])
        disc += to_decimal(r["discount_amount"])
    return qty, disc


def _return_prior_sums(conn, return_against_id, item_id):
    """Prior returned (qty, discount) for one billed item, as positives.

    PyPika with bound parameters; sums in Python Decimal, never in SQL.
    Counts return lines on posted debit notes against the bill; stored
    return quantities and discounts are negative, so both sums are negated.
    """
    pii_t = Table("purchase_invoice_item")
    pi_t = Table("purchase_invoice")
    q = (Q.from_(pii_t)
         .join(pi_t).on(pi_t.id == pii_t.purchase_invoice_id)
         .select(pii_t.quantity, pii_t.discount_amount)
         .where(pii_t.item_id == P())
         .where(pi_t.return_against == P())
         .where(pi_t.is_return == 1)
         .where(pi_t.status != ValueWrapper("draft"))
         .where(pi_t.status != ValueWrapper("cancelled")))
    rows = conn.execute(q.get_sql(), (item_id, return_against_id)).fetchall()
    ret_qty = Decimal("0")
    ret_disc = Decimal("0")
    for r in rows:
        ret_qty -= to_decimal(r["quantity"])
        ret_disc -= to_decimal(r["discount_amount"])
    return ret_qty, ret_disc


def _preview_bill_discount(conn, poi_row, qty, intra_qty, intra_disc):
    """Preview share for one bill line, accumulating intra-document prior."""
    po_item_id = poi_row["id"]
    qty = to_decimal(qty)
    order_qty = to_decimal(poi_row["quantity"])
    order_disc = round_currency(
        to_decimal(poi_row["amount"]) - to_decimal(poi_row["net_amount"]))
    sub_qty, sub_disc = _bill_prior_sums(conn, po_item_id)
    prior_qty = sub_qty + intra_qty.get(po_item_id, Decimal("0"))
    prior_disc = sub_disc + intra_disc.get(po_item_id, Decimal("0"))
    share = discount_share(order_disc, order_qty, prior_qty, prior_disc, qty)
    intra_qty[po_item_id] = intra_qty.get(po_item_id, Decimal("0")) + qty
    intra_disc[po_item_id] = intra_disc.get(po_item_id, Decimal("0")) + share
    return share


def check_fifo_discounted_net(conn, item_id, net, qty_text):
    """Refuse a discounted stock line no per-unit rate can hold.

    Shared by receipt submit and stock-moving bill submit: `net` is the
    line's discounted net as a Decimal, `qty_text` its stored quantity
    string. Only discounted lines reach here. When the item is FIFO-valued
    and `round_currency(net / qty) * qty != net`, no per-unit rate
    reproduces the net, so the caller refuses loudly instead of booking a
    rounded rate. Returns the refusal message, or None when the net
    divides. Never exits: receipt submit reports it through `err()`,
    while bill submit raises `_SubmitRefused` so an in-process caller
    (such as the recurring-bill generator) is not killed mid-run.
    """
    item_t = Table("item")
    q = (Q.from_(item_t).select(item_t.valuation_method)
         .where(item_t.id == P()))
    row = conn.execute(q.get_sql(), (item_id,)).fetchone()
    if row is None:
        return None
    if (row["valuation_method"] or "moving_average") != "fifo":
        return None
    qty = to_decimal(qty_text)
    if qty == 0:
        return None
    if round_currency(net / qty) * qty != net:
        return (f"Item {item_id} is FIFO-valued and its discounted net {net:.2f} "
                f"for {qty_text} units cannot be held at a per-unit rate; "
                f"receive it undiscounted or wait for FIFO layer values")
    return None


# ---------------------------------------------------------------------------
# 1. add-supplier
# ---------------------------------------------------------------------------

def add_supplier(conn, args):
    """Create a supplier record."""
    if not args.name:
        err("--name is required")
    if not args.company_id:
        err("--company-id is required")

    company_t = Table("company")
    q = Q.from_(company_t).select(company_t.id, company_t.default_currency).where(company_t.id == P())
    company_row = conn.execute(q.get_sql(), (args.company_id,)).fetchone()
    if not company_row:
        err(f"Company {args.company_id} not found")
    company_currency = company_row["default_currency"]

    supplier_type = args.supplier_type or "company"
    if supplier_type not in ("company", "individual"):
        err("--supplier-type must be 'company' or 'individual'")

    if args.payment_terms_id:
        pt_t = Table("payment_terms")
        q = Q.from_(pt_t).select(pt_t.id).where(pt_t.id == P())
        if not conn.execute(q.get_sql(), (args.payment_terms_id,)).fetchone():
            err(f"Payment terms {args.payment_terms_id} not found")

    primary_address = args.primary_address
    if primary_address:
        _parse_json_arg(primary_address, "primary-address")

    is_1099 = int(args.is_1099_vendor) if args.is_1099_vendor else 0

    supplier_id = str(uuid.uuid4())
    try:
        s_t = Table("supplier")
        q = (Q.into(s_t)
             .columns("id", "name", "supplier_group", "supplier_type",
                      "payment_terms_id", "tax_id", "is_1099_vendor",
                      "primary_address", "email", "phone", "status",
                      "default_currency", "company_id")
             .insert(P(), P(), P(), P(), P(), P(), P(), P(),
                     P(), P(), ValueWrapper("active"), P(), P()))
        conn.execute(q.get_sql(),
            (supplier_id, args.name, args.supplier_group, supplier_type,
             args.payment_terms_id, args.tax_id, is_1099,
             primary_address, getattr(args, "email", None),
             getattr(args, "phone", None), company_currency, args.company_id))
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-buying] {e}\n")
        err("Supplier creation failed — check for duplicates or invalid data")

    cf_errors = store_from_arg(conn, "supplier", supplier_id, getattr(args, "custom_fields", None))
    if cf_errors:
        conn.rollback()
        err("Custom field error: " + "; ".join(cf_errors))

    audit(conn, "erpclaw-buying", "add-supplier", "supplier", supplier_id,
           new_values={"name": args.name, "type": supplier_type})
    conn.commit()
    resp = {"supplier_id": supplier_id, "name": args.name}
    ok(merge_into_response(conn, "supplier", supplier_id, resp))


# ---------------------------------------------------------------------------
# 2. update-supplier
# ---------------------------------------------------------------------------

def update_supplier(conn, args):
    """Update a supplier."""
    if not args.supplier_id:
        err("--supplier-id is required")

    s_t = Table("supplier")
    q = (Q.from_(s_t).select(s_t.star)
         .where((s_t.id == P()) | (s_t.name == P())))
    supplier = conn.execute(q.get_sql(),
                            (args.supplier_id, args.supplier_id)).fetchone()
    if not supplier:
        err(f"Supplier {args.supplier_id} not found",
             suggestion="Use 'list suppliers' to see available suppliers.")
    args.supplier_id = supplier["id"]  # normalize to id

    data, updated_fields = {}, []

    if args.name is not None:
        data["name"] = args.name
        updated_fields.append("name")
    if args.payment_terms_id is not None:
        data["payment_terms_id"] = args.payment_terms_id
        updated_fields.append("payment_terms_id")
    if args.supplier_group is not None:
        data["supplier_group"] = args.supplier_group
        updated_fields.append("supplier_group")
    if args.supplier_type is not None:
        if args.supplier_type not in ("company", "individual"):
            err("--supplier-type must be 'company' or 'individual'")
        data["supplier_type"] = args.supplier_type
        updated_fields.append("supplier_type")
    if getattr(args, "email", None) is not None:
        data["email"] = args.email
        updated_fields.append("email")
    if getattr(args, "phone", None) is not None:
        data["phone"] = args.phone
        updated_fields.append("phone")

    if not updated_fields:
        err("No fields to update")

    data["updated_at"] = now()
    sql, params = dynamic_update("supplier", data, where={"id": args.supplier_id})
    conn.execute(sql, params)

    audit(conn, "erpclaw-buying", "update-supplier", "supplier", args.supplier_id,
           new_values={"updated_fields": updated_fields})
    conn.commit()
    ok({"supplier_id": args.supplier_id, "updated_fields": updated_fields})


# ---------------------------------------------------------------------------
# 3. get-supplier
# ---------------------------------------------------------------------------

def get_supplier(conn, args):
    """Get supplier with outstanding summary."""
    if not args.supplier_id:
        err("--supplier-id is required")

    scope_company_id = None
    if getattr(args, "company_id", None) or getattr(args, "company_name", None):
        scope_company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))

    s_t = Table("supplier")
    if scope_company_id:
        q = (Q.from_(s_t).select(s_t.star)
             .where((s_t.id == P()) | ((s_t.name == P()) & (s_t.company_id == P()))))
        supplier = conn.execute(q.get_sql(),
                                (args.supplier_id, args.supplier_id, scope_company_id)).fetchone()
    else:
        q = (Q.from_(s_t).select(s_t.star)
             .where((s_t.id == P()) | (s_t.name == P())))
        supplier = conn.execute(q.get_sql(),
                                (args.supplier_id, args.supplier_id)).fetchone()
    if not supplier:
        err(f"Supplier {args.supplier_id} not found")
    if scope_company_id and supplier["company_id"] != scope_company_id:
        err(f"Supplier {args.supplier_id} belongs to another company")

    data = row_to_dict(supplier)

    # Outstanding from purchase invoices
    pi_t = Table("purchase_invoice")
    q = (Q.from_(pi_t)
         .select(fn.Coalesce(DecimalSum(pi_t.outstanding_amount), ValueWrapper("0")).as_("total_outstanding"),
                 fn.Count("*").as_("invoice_count"))
         .where(pi_t.supplier_id == P())
         .where(pi_t.status.isin([P(), P(), P()])))
    outstanding = conn.execute(q.get_sql(),
        (args.supplier_id, "submitted", "overdue", "partially_paid")).fetchone()
    data["total_outstanding"] = str(round_currency(to_decimal(str(outstanding["total_outstanding"]))))
    data["outstanding_invoice_count"] = outstanding["invoice_count"]

    ok(merge_into_response(conn, "supplier", supplier["id"], data))


# ---------------------------------------------------------------------------
# 4. list-suppliers
# ---------------------------------------------------------------------------

def list_suppliers(conn, args):
    """List suppliers with filtering."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))
    s = Table("supplier").as_("s")
    params = []

    count_q = Q.from_(s).select(fn.Count("*"))
    data_q = (Q.from_(s)
              .select(s.id, s.name, s.supplier_group, s.supplier_type,
                      s.tax_id, s.is_1099_vendor, s.status, s.company_id))

    count_q = count_q.where(s.company_id == P())
    data_q = data_q.where(s.company_id == P())
    params.append(company_id)
    if args.supplier_group:
        count_q = count_q.where(s.supplier_group == P())
        data_q = data_q.where(s.supplier_group == P())
        params.append(args.supplier_group)
    if args.search:
        crit = (s.name.like(P())) | (s.tax_id.like(P()))
        count_q = count_q.where(crit)
        data_q = data_q.where(crit)
        params.extend([f"%{args.search}%", f"%{args.search}%"])

    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    data_params = params + [limit, offset]

    data_q = data_q.orderby(s.name).limit(P()).offset(P())
    rows = conn.execute(data_q.get_sql(), data_params).fetchall()

    ok({"suppliers": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 5. add-material-request
# ---------------------------------------------------------------------------

def add_material_request(conn, args):
    """Create a material request in draft."""
    if not args.request_type:
        err("--request-type is required (purchase|transfer|manufacture)")
    valid_types = ("purchase", "transfer", "manufacture",
                   "material_transfer", "material_issue")
    rtype = args.request_type
    if rtype == "transfer":
        rtype = "material_transfer"
    if rtype not in ("purchase", "material_transfer", "material_issue", "manufacture"):
        err(f"--request-type must be one of: purchase, transfer, manufacture")
    if not args.items:
        err("--items is required (JSON array)")
    if not args.company_id:
        err("--company-id is required")

    company_t = Table("company")
    q = Q.from_(company_t).select(company_t.id).where(company_t.id == P())
    if not conn.execute(q.get_sql(), (args.company_id,)).fetchone():
        err(f"Company {args.company_id} not found")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    mr_id = str(uuid.uuid4())

    # Insert parent first (FK target)
    mr_t = Table("material_request")
    q = (Q.into(mr_t)
         .columns("id", "request_type", "status", "company_id")
         .insert(P(), P(), ValueWrapper("draft"), P()))
    conn.execute(q.get_sql(), (mr_id, rtype, args.company_id))

    mri_t = Table("material_request_item")
    mri_q = (Q.into(mri_t)
             .columns("id", "material_request_id", "item_id", "quantity", "warehouse_id")
             .insert(P(), P(), P(), P(), P()))
    mri_sql = mri_q.get_sql()

    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        qty = to_decimal(item.get("qty", "0"))
        if qty <= 0:
            err(f"Item {i}: qty must be > 0")

        conn.execute(mri_sql,
            (str(uuid.uuid4()), mr_id, item_id, str(round_currency(qty)),
             item.get("warehouse_id")))

    audit(conn, "erpclaw-buying", "add-material-request", "material_request", mr_id,
           new_values={"request_type": rtype, "item_count": len(items)})
    conn.commit()
    ok({"material_request_id": mr_id, "request_type": rtype,
         "item_count": len(items)})


# ---------------------------------------------------------------------------
# 6. submit-material-request
# ---------------------------------------------------------------------------

def submit_material_request(conn, args):
    """Submit a material request."""
    if not args.material_request_id:
        err("--material-request-id is required")

    mr_t = Table("material_request")
    q = Q.from_(mr_t).select(mr_t.star).where(mr_t.id == P())
    mr = conn.execute(q.get_sql(), (args.material_request_id,)).fetchone()
    if not mr:
        err(f"Material request {args.material_request_id} not found")
    if mr["status"] != "draft":
        err(f"Cannot submit: material request is '{mr['status']}' (must be 'draft')")

    naming = get_next_name(conn, "material_request", company_id=mr["company_id"])

    q = (Q.update(mr_t)
         .set(mr_t.status, ValueWrapper("submitted"))
         .set(mr_t.naming_series, P())
         .set(mr_t.updated_at, now())
         .where(mr_t.id == P()))
    conn.execute(q.get_sql(), (naming, args.material_request_id))

    audit(conn, "erpclaw-buying", "submit-material-request", "material_request",
           args.material_request_id,
           new_values={"naming_series": naming})
    conn.commit()
    ok({"material_request_id": args.material_request_id,
         "naming_series": naming, "status": "submitted"})


# ---------------------------------------------------------------------------
# 7. list-material-requests
# ---------------------------------------------------------------------------

def list_material_requests(conn, args):
    """List material requests."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))
    mr = Table("material_request").as_("mr")
    params = []

    count_q = Q.from_(mr).select(fn.Count("*"))
    data_q = Q.from_(mr).select(mr.star)

    count_q = count_q.where(mr.company_id == P())
    data_q = data_q.where(mr.company_id == P())
    params.append(company_id)
    if args.request_type:
        rtype = args.request_type
        if rtype == "transfer":
            rtype = "material_transfer"
        count_q = count_q.where(mr.request_type == P())
        data_q = data_q.where(mr.request_type == P())
        params.append(rtype)
    if args.mr_status:
        count_q = count_q.where(mr.status == P())
        data_q = data_q.where(mr.status == P())
        params.append(args.mr_status)

    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    data_params = params + [limit, offset]

    data_q = data_q.orderby(mr.created_at, order=Order.desc).limit(P()).offset(P())
    rows = conn.execute(data_q.get_sql(), data_params).fetchall()

    ok({"material_requests": [row_to_dict(r) for r in rows],
         "total_count": total_count, "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# get-material-request (WS2/D2)
# ---------------------------------------------------------------------------

def get_material_request(conn, args):
    """Get a material request with its items."""
    if not args.material_request_id:
        err("--material-request-id is required")

    mr_t = Table("material_request")
    q = Q.from_(mr_t).select(mr_t.star).where(mr_t.id == P())
    mr = conn.execute(q.get_sql(), (args.material_request_id,)).fetchone()
    if not mr:
        err(f"Material request {args.material_request_id} not found")

    data = row_to_dict(mr)

    mri = Table("material_request_item").as_("mri")
    i_t = Table("item").as_("i")
    q = (Q.from_(mri)
         .left_join(i_t).on(i_t.id == mri.item_id)
         .select(mri.star, i_t.item_code, i_t.item_name)
         .where(mri.material_request_id == P())
         .orderby(line_order(mri)))
    items = conn.execute(q.get_sql(), (args.material_request_id,)).fetchall()
    data["items"] = [row_to_dict(r) for r in items]

    ok(data)


# ---------------------------------------------------------------------------
# 8. add-rfq
# ---------------------------------------------------------------------------

def add_rfq(conn, args):
    """Create a Request for Quotation."""
    if not args.items:
        err("--items is required (JSON array)")
    if not args.suppliers:
        err("--suppliers is required (JSON array of supplier IDs)")
    if not args.company_id:
        err("--company-id is required")

    company_t = Table("company")
    q = Q.from_(company_t).select(company_t.id).where(company_t.id == P())
    if not conn.execute(q.get_sql(), (args.company_id,)).fetchone():
        err(f"Company {args.company_id} not found")

    items = _parse_json_arg(args.items, "items")
    suppliers = _parse_json_arg(args.suppliers, "suppliers")

    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")
    if not suppliers or not isinstance(suppliers, list):
        err("--suppliers must be a non-empty JSON array")

    # Validate suppliers exist
    sup_t = Table("supplier")
    sup_q = Q.from_(sup_t).select(sup_t.id).where(sup_t.id == P())
    sup_sql = sup_q.get_sql()
    for sid in suppliers:
        if not conn.execute(sup_sql, (sid,)).fetchone():
            err(f"Supplier {sid} not found")

    rfq_id = str(uuid.uuid4())
    today = _today()

    # Insert parent first
    rfq_t = Table("request_for_quotation")
    q = (Q.into(rfq_t)
         .columns("id", "rfq_date", "status", "company_id")
         .insert(P(), P(), ValueWrapper("draft"), P()))
    conn.execute(q.get_sql(), (rfq_id, today, args.company_id))

    # Insert RFQ items
    ri_t = Table("rfq_item")
    ri_q = (Q.into(ri_t)
            .columns("id", "rfq_id", "item_id", "quantity", "uom", "required_date")
            .insert(P(), P(), P(), P(), P(), P()))
    ri_sql = ri_q.get_sql()
    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        qty = to_decimal(item.get("qty", "0"))
        if qty <= 0:
            err(f"Item {i}: qty must be > 0")
        conn.execute(ri_sql,
            (str(uuid.uuid4()), rfq_id, item_id, str(round_currency(qty)),
             item.get("uom"), item.get("required_date")))

    # Insert RFQ suppliers
    rs_t = Table("rfq_supplier")
    rs_q = (Q.into(rs_t)
            .columns("id", "rfq_id", "supplier_id")
            .insert(P(), P(), P()))
    rs_sql = rs_q.get_sql()
    for sid in suppliers:
        conn.execute(rs_sql, (str(uuid.uuid4()), rfq_id, sid))

    audit(conn, "erpclaw-buying", "add-rfq", "request_for_quotation", rfq_id,
           new_values={"item_count": len(items), "supplier_count": len(suppliers)})
    conn.commit()
    ok({"rfq_id": rfq_id, "item_count": len(items),
         "supplier_count": len(suppliers)})


# ---------------------------------------------------------------------------
# 9. submit-rfq
# ---------------------------------------------------------------------------

RFQ_REQUEST_MAX_SUPPLIERS = 100
RFQ_REQUEST_MAX_LINES = 100
RFQ_REQUEST_MAX_FIELD = 200
RFQ_REQUEST_MAX_BODY_BYTES = 32768
RFQ_REQUEST_MAX_SNAPSHOT_BYTES = 1048576


def _reviewed_rfq(conn, args):
    rfq_id = getattr(args, "rfq_id", None)
    company_id = getattr(args, "company_id", None)
    if not rfq_id or not company_id:
        err("--rfq-id and --company-id are required")
    if any(not isinstance(value, str) or len(value) > RFQ_REQUEST_MAX_FIELD
           for value in (rfq_id, company_id)):
        err("RFQ and company identifiers must be bounded text")
    rfq_t = Table("request_for_quotation")
    query = (Q.from_(rfq_t).select(rfq_t.star).where(rfq_t.id == P())
             .where(rfq_t.company_id == P()))
    row = conn.execute(query.get_sql(), (rfq_id, company_id)).fetchone()
    if row is None:
        err("RFQ not found in the selected company")
    return row


def create_rfq_supplier_request(conn, args):
    """Prepare and retain unsent supplier requests for explicit human review."""
    rfq = _reviewed_rfq(conn, args)
    if rfq["status"] not in ("draft", "submitted", "quotation_received"):
        err("Cannot prepare communications for a cancelled RFQ")
    kind = getattr(args, "communication_kind", None) or "request"
    if kind not in ("request", "reminder"):
        err("--communication-kind must be request or reminder")
    rs, supplier = Table("rfq_supplier"), Table("supplier")
    query = (Q.from_(rs).join(Table("request_for_quotation"))
             .on(rs.rfq_id == Table("request_for_quotation").id)
             .left_join(supplier).on((rs.supplier_id == supplier.id)
                                    & (supplier.company_id == P()))
             .select(rs.supplier_id, rs.response_date, rs.supplier_quotation_id,
                     supplier.id.as_("matched_supplier_id"), supplier.name,
                     supplier.email, supplier.status, supplier.company_id)
             .where(rs.rfq_id == P())
             .where(Table("request_for_quotation").company_id == P())
             .orderby(rs.supplier_id).limit(RFQ_REQUEST_MAX_SUPPLIERS + 1))
    suppliers = conn.execute(query.get_sql(), (
        rfq["company_id"], rfq["id"], rfq["company_id"])).fetchall()
    if not suppliers or len(suppliers) > RFQ_REQUEST_MAX_SUPPLIERS:
        err("RFQ must have between 1 and 100 assigned suppliers")
    for row in suppliers:
        if row["matched_supplier_id"] is None or row["status"] != "active":
            err("Assigned suppliers must exist, be active and belong to the RFQ company")
    selected = getattr(args, "supplier_id", None)
    if selected:
        suppliers = [row for row in suppliers if row["supplier_id"] == selected]
    if not suppliers:
        err("The selected supplier must be assigned to the RFQ")
    ri, item = Table("rfq_item"), Table("item")
    parent = Table("request_for_quotation")
    query = (Q.from_(ri).join(parent).on(ri.rfq_id == parent.id)
             .left_join(item).on(ri.item_id == item.id)
             .select(ri.item_id, ri.quantity, ri.uom, ri.required_date,
                     item.item_name, item.stock_uom, item.status)
             .where(ri.rfq_id == P()).where(parent.company_id == P())
             .orderby(ri.id).limit(RFQ_REQUEST_MAX_LINES + 1))
    item_rows = conn.execute(query.get_sql(), (rfq["id"], rfq["company_id"])).fetchall()
    if not item_rows or len(item_rows) > RFQ_REQUEST_MAX_LINES:
        err("RFQ must have between 1 and 100 item lines")

    def reviewed_text(value, name):
        if (not isinstance(value, str) or not value.strip()
                or len(value) > RFQ_REQUEST_MAX_FIELD
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            err(f"{name} must be reviewed single-line text of at most 200 characters")
        return value

    lines = []
    for row in item_rows:
        if row["item_name"] is None or row["status"] == "disabled":
            err("RFQ item must identify an active item")
        qty = row["quantity"]
        if (not isinstance(qty, str)
                or re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", qty) is None
                or Decimal(qty) <= 0):
            err("RFQ quantities must be positive Decimal text with at most two decimal places")
        required_date = row["required_date"]
        if required_date is not None:
            try:
                if date_type.fromisoformat(required_date).isoformat() != required_date:
                    raise ValueError()
            except (TypeError, ValueError):
                err("RFQ required dates must be ISO dates")
        lines.append({"item_id": reviewed_text(row["item_id"], "Item identifier"),
                      "item_name": reviewed_text(row["item_name"], "Item name"),
                      "qty": str(round_currency(Decimal(qty))),
                      "uom": reviewed_text(row["uom"] or row["stock_uom"], "Item UOM"),
                      "required_date": required_date})
    label = reviewed_text(rfq["naming_series"] or rfq["id"], "RFQ reference")
    drafts, skipped = [], []
    for row in suppliers:
        supplier_id = reviewed_text(row["supplier_id"], "Supplier identifier")
        if kind == "reminder" and (row["response_date"] or row["supplier_quotation_id"]):
            skipped.append(supplier_id)
            continue
        recipient = reviewed_text(row["email"], "Supplier email")
        if re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", recipient) is None:
            err("Assigned supplier needs a reviewed email address")
        supplier_name = reviewed_text(row["name"], "Supplier name")
        subject = ("Quotation request " if kind == "request" else "Quotation reminder ") + label
        body_lines = [f"Hello {supplier_name},", f"Please quote for RFQ {label}:"]
        for line in lines:
            due = f"; required {line['required_date']}" if line["required_date"] else ""
            body_lines.append(f"{line['item_name']}: {line['qty']} {line['uom']}{due}")
        body_lines.append("Please provide unit prices, availability and delivery dates for review.")
        body = "\n".join(body_lines)
        if len(body.encode("utf-8")) > RFQ_REQUEST_MAX_BODY_BYTES:
            err("Prepared supplier message exceeds the 32768-byte limit")
        drafts.append({"supplier_id": supplier_id, "to": recipient,
                       "subject": subject, "body": body,
                       "state": "prepared-not-sent"})
    if not drafts:
        err("No suppliers are awaiting a quotation response")
    prepared = {"rfq_id": rfq["id"], "company_id": rfq["company_id"],
                "communication_kind": kind, "items": lines, "drafts": drafts,
                "responded_suppliers_skipped": skipped, "state": "prepared-not-sent"}
    snapshot = json.dumps(prepared, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"))
    if len(snapshot.encode("utf-8")) > RFQ_REQUEST_MAX_SNAPSHOT_BYTES:
        err("Prepared supplier snapshot exceeds the 1048576-byte limit")
    draft_id = str(uuid.uuid4())
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    prepared_at = datetime.now(timezone.utc).isoformat()
    table = Table("rfq_supplier_request")
    query = Q.into(table).columns(
        "id", "company_id", "rfq_id", "prepared_at", "snapshot", "content_sha256"
    ).insert(P(), P(), P(), P(), P(), P())
    scope_note = company_scope.bound_note(conn)
    scope_status = (scope_note.status if scope_note is not None
                    else company_scope.NO_PRINCIPAL)
    try:
        conn.execute("BEGIN")
        conn.execute(query.get_sql(), (draft_id, rfq["company_id"], rfq["id"],
                                      prepared_at, snapshot, digest))
        audit(conn, "erpclaw-buying", "create-rfq-supplier-request",
              "request_for_quotation", rfq["id"],
              new_values={"draft_id": draft_id, "content_sha256": digest,
                          "state": "prepared-not-sent"},
              scope_company_ids=[rfq["company_id"]],
              scope_status=scope_status,
              description="Prepared supplier communications; nothing sent")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    ok(prepared)


def list_rfq_supplier_requests(conn, args):
    """Read company-scoped unsent preparations without changing any state."""
    rfq = _reviewed_rfq(conn, args)
    table = Table("rfq_supplier_request")
    query = (Q.from_(table).select(table.id, table.prepared_at, table.snapshot)
             .where(table.company_id == P()).where(table.rfq_id == P())
             .orderby(table.prepared_at, order=Order.desc)
             .orderby(table.id, order=Order.desc).limit(20))
    rows = conn.execute(query.get_sql(), (rfq["company_id"], rfq["id"])).fetchall()
    snapshots = [{"draft_id": row["id"], "prepared_at": row["prepared_at"],
                  "preparation": json.loads(row["snapshot"])} for row in rows]
    ok({"rfq_id": rfq["id"], "company_id": rfq["company_id"],
        "preparations": snapshots, "count": len(snapshots)})


def submit_rfq(conn, args):
    """Submit an RFQ."""
    if not args.rfq_id:
        err("--rfq-id is required")

    rfq_t = Table("request_for_quotation")
    q = Q.from_(rfq_t).select(rfq_t.star).where(rfq_t.id == P())
    rfq = conn.execute(q.get_sql(), (args.rfq_id,)).fetchone()
    if not rfq:
        err(f"RFQ {args.rfq_id} not found")
    if rfq["status"] != "draft":
        err(f"Cannot submit: RFQ is '{rfq['status']}' (must be 'draft')")

    naming = get_next_name(conn, "request_for_quotation",
                           company_id=rfq["company_id"])

    q = (Q.update(rfq_t)
         .set(rfq_t.status, ValueWrapper("submitted"))
         .set(rfq_t.naming_series, P())
         .set(rfq_t.updated_at, now())
         .where(rfq_t.id == P()))
    conn.execute(q.get_sql(), (naming, args.rfq_id))

    # Mark sent_date on rfq_supplier rows
    rs_t = Table("rfq_supplier")
    q = (Q.update(rs_t)
         .set(rs_t.sent_date, now())
         .where(rs_t.rfq_id == P()))
    conn.execute(q.get_sql(), (args.rfq_id,))

    audit(conn, "erpclaw-buying", "submit-rfq", "request_for_quotation", args.rfq_id,
           new_values={"naming_series": naming})
    conn.commit()
    ok({"rfq_id": args.rfq_id, "naming_series": naming,
         "status": "submitted"})


# ---------------------------------------------------------------------------
# 10. list-rfqs
# ---------------------------------------------------------------------------

def list_rfqs(conn, args):
    """List RFQs."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))
    r = Table("request_for_quotation").as_("r")
    params = []

    count_q = Q.from_(r).select(fn.Count("*"))
    data_q = Q.from_(r).select(r.star)

    count_q = count_q.where(r.company_id == P())
    data_q = data_q.where(r.company_id == P())
    params.append(company_id)
    if args.rfq_status:
        count_q = count_q.where(r.status == P())
        data_q = data_q.where(r.status == P())
        params.append(args.rfq_status)

    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    data_params = params + [limit, offset]

    data_q = data_q.orderby(r.created_at, order=Order.desc).limit(P()).offset(P())
    rows = conn.execute(data_q.get_sql(), data_params).fetchall()

    ok({"rfqs": [row_to_dict(r_row) for r_row in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 11. add-supplier-quotation
# ---------------------------------------------------------------------------

def add_supplier_quotation(conn, args):
    """Record a supplier's quotation response to an RFQ."""
    if not args.rfq_id:
        err("--rfq-id is required")
    if not args.supplier_id:
        err("--supplier-id is required")
    if not args.items:
        err("--items is required (JSON array with prices)")

    rfq_t = Table("request_for_quotation")
    q = Q.from_(rfq_t).select(rfq_t.star).where(rfq_t.id == P())
    rfq = conn.execute(q.get_sql(), (args.rfq_id,)).fetchone()
    if not rfq:
        err(f"RFQ {args.rfq_id} not found")

    sup_t = Table("supplier")
    q = (Q.from_(sup_t).select(sup_t.star)
         .where((sup_t.id == P()) | (sup_t.name == P())))
    supplier = conn.execute(q.get_sql(),
                            (args.supplier_id, args.supplier_id)).fetchone()
    if not supplier:
        err(f"Supplier {args.supplier_id} not found")
    args.supplier_id = supplier["id"]  # normalize to id

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    sq_id = str(uuid.uuid4())
    today = _today()
    total_amount = Decimal("0")

    # Insert parent first
    sq_t = Table("supplier_quotation")
    q = (Q.into(sq_t)
         .columns("id", "supplier_id", "quotation_date", "rfq_id",
                  "total_amount", "grand_total", "status", "company_id")
         .insert(P(), P(), P(), P(), ValueWrapper("0"), ValueWrapper("0"),
                 ValueWrapper("draft"), P()))
    conn.execute(q.get_sql(),
        (sq_id, args.supplier_id, today, args.rfq_id, rfq["company_id"]))

    ri_t = Table("rfq_item")
    ri_q = Q.from_(ri_t).select(ri_t.star).where(ri_t.id == P())
    ri_sql = ri_q.get_sql()

    sqi_t = Table("supplier_quotation_item")
    sqi_q = (Q.into(sqi_t)
             .columns("id", "supplier_quotation_id", "item_id", "quantity",
                      "rate", "amount", "lead_time_days")
             .insert(P(), P(), P(), P(), P(), P(), P()))
    sqi_sql = sqi_q.get_sql()

    for i, item in enumerate(items):
        rfq_item_id = item.get("rfq_item_id")
        if not rfq_item_id:
            err(f"Item {i}: rfq_item_id is required")
        rate = to_decimal(item.get("rate", "0"))
        if rate <= 0:
            err(f"Item {i}: rate must be > 0")

        # Get qty from the rfq_item
        rfq_item = conn.execute(ri_sql, (rfq_item_id,)).fetchone()
        if not rfq_item:
            err(f"Item {i}: rfq_item {rfq_item_id} not found")

        qty = to_decimal(rfq_item["quantity"])
        amount = round_currency(qty * rate)
        total_amount += amount

        conn.execute(sqi_sql,
            (str(uuid.uuid4()), sq_id, rfq_item["item_id"],
             str(round_currency(qty)), str(round_currency(rate)),
             str(amount), item.get("lead_time_days")))

    # Update totals
    q = (Q.update(sq_t)
         .set(sq_t.total_amount, P())
         .set(sq_t.grand_total, P())
         .where(sq_t.id == P()))
    conn.execute(q.get_sql(),
        (str(round_currency(total_amount)), str(round_currency(total_amount)), sq_id))

    # Mark rfq_supplier as having a response
    rs_t = Table("rfq_supplier")
    q = (Q.update(rs_t)
         .set(rs_t.response_date, now())
         .set(rs_t.supplier_quotation_id, P())
         .where(rs_t.rfq_id == P())
         .where(rs_t.supplier_id == P()))
    conn.execute(q.get_sql(), (sq_id, args.rfq_id, args.supplier_id))

    # Update RFQ status if all suppliers responded
    q = (Q.from_(rs_t)
         .select(fn.Count("*").as_("total"),
                 fn.Sum(Case().when(rs_t.supplier_quotation_id.isnotnull(), 1).else_(0)).as_("responded"))
         .where(rs_t.rfq_id == P()))
    all_responded = conn.execute(q.get_sql(), (args.rfq_id,)).fetchone()
    if all_responded["total"] == all_responded["responded"]:
        q = (Q.update(rfq_t)
             .set(rfq_t.status, ValueWrapper("quotation_received"))
             .set(rfq_t.updated_at, now())
             .where(rfq_t.id == P()))
        conn.execute(q.get_sql(), (args.rfq_id,))

    audit(conn, "erpclaw-buying", "add-supplier-quotation", "supplier_quotation", sq_id,
           new_values={"supplier_id": args.supplier_id, "rfq_id": args.rfq_id,
                       "total_amount": str(round_currency(total_amount))})
    conn.commit()
    ok({"supplier_quotation_id": sq_id,
         "total_amount": str(round_currency(total_amount))})


# ---------------------------------------------------------------------------
# 12. list-supplier-quotations
# ---------------------------------------------------------------------------

def list_supplier_quotations(conn, args):
    """List supplier quotations."""
    sq = Table("supplier_quotation").as_("sq")
    s = Table("supplier").as_("s")
    params = []

    count_q = Q.from_(sq).select(fn.Count("*"))
    data_q = (Q.from_(sq)
              .left_join(s).on(s.id == sq.supplier_id)
              .select(sq.star, s.name.as_("supplier_name")))

    if args.rfq_id:
        count_q = count_q.where(sq.rfq_id == P())
        data_q = data_q.where(sq.rfq_id == P())
        params.append(args.rfq_id)
    if args.supplier_id:
        count_q = count_q.where(sq.supplier_id == P())
        data_q = data_q.where(sq.supplier_id == P())
        params.append(args.supplier_id)

    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    data_params = params + [limit, offset]

    data_q = data_q.orderby(sq.created_at, order=Order.desc).limit(P()).offset(P())
    rows = conn.execute(data_q.get_sql(), data_params).fetchall()

    ok({"supplier_quotations": [row_to_dict(r) for r in rows],
         "total_count": total_count, "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 13. compare-supplier-quotations
# ---------------------------------------------------------------------------

def compare_supplier_quotations(conn, args):
    """Compare supplier quotes for the same RFQ items side by side."""
    if not args.rfq_id:
        err("--rfq-id is required")

    rfq_t = Table("request_for_quotation")
    q = Q.from_(rfq_t).select(rfq_t.star).where(rfq_t.id == P())
    rfq = conn.execute(q.get_sql(), (args.rfq_id,)).fetchone()
    if not rfq:
        err(f"RFQ {args.rfq_id} not found")

    # Get all RFQ items
    ri = Table("rfq_item").as_("ri")
    i_t = Table("item").as_("i")
    q = (Q.from_(ri)
         .left_join(i_t).on(i_t.id == ri.item_id)
         .select(ri.star, i_t.item_code, i_t.item_name)
         .where(ri.rfq_id == P()))
    rfq_items = conn.execute(q.get_sql(), (args.rfq_id,)).fetchall()

    # Get all supplier quotations for this RFQ
    sq_t = Table("supplier_quotation").as_("sq")
    s_t = Table("supplier").as_("s")
    q = (Q.from_(sq_t)
         .left_join(s_t).on(s_t.id == sq_t.supplier_id)
         .select(sq_t.star, s_t.name.as_("supplier_name"))
         .where(sq_t.rfq_id == P()))
    sqs = conn.execute(q.get_sql(), (args.rfq_id,)).fetchall()

    sqi_t = Table("supplier_quotation_item")
    sqi_q = (Q.from_(sqi_t).select(sqi_t.star)
             .where(sqi_t.supplier_quotation_id == P())
             .where(sqi_t.item_id == P()))
    sqi_sql = sqi_q.get_sql()

    comparison = []
    for ri_row in rfq_items:
        ri_d = row_to_dict(ri_row)
        item_comparison = {
            "item_id": ri_d["item_id"],
            "item_code": ri_d.get("item_code"),
            "item_name": ri_d.get("item_name"),
            "required_qty": ri_d["quantity"],
            "quotes": [],
            "lowest_rate": None,
            "lowest_supplier": None,
        }
        lowest_rate = None
        for sq_row in sqs:
            sq = row_to_dict(sq_row)
            # Find the quote item for this RFQ item
            sqi = conn.execute(sqi_sql,
                (sq["id"], ri_d["item_id"])).fetchone()
            if sqi:
                sqi_d = row_to_dict(sqi)
                rate = to_decimal(sqi_d["rate"])
                quote_info = {
                    "supplier_id": sq["supplier_id"],
                    "supplier_name": sq.get("supplier_name"),
                    "rate": sqi_d["rate"],
                    "amount": sqi_d["amount"],
                    "lead_time_days": sqi_d.get("lead_time_days"),
                    "is_lowest": False,
                }
                item_comparison["quotes"].append(quote_info)
                if lowest_rate is None or rate < lowest_rate:
                    lowest_rate = rate
                    item_comparison["lowest_rate"] = str(round_currency(rate))
                    item_comparison["lowest_supplier"] = sq.get("supplier_name")

        # Mark lowest
        for q in item_comparison["quotes"]:
            if item_comparison["lowest_rate"] and q["rate"] == item_comparison["lowest_rate"]:
                q["is_lowest"] = True

        comparison.append(item_comparison)

    ok({"rfq_id": args.rfq_id, "comparison": comparison,
         "supplier_count": len(sqs)})


# ---------------------------------------------------------------------------
# 14. add-purchase-order
# ---------------------------------------------------------------------------

def add_purchase_order(conn, args):
    """Create a purchase order in draft."""
    if not args.supplier_id:
        err("--supplier-id is required")
    if not args.items:
        err("--items is required (JSON array)")
    if not args.company_id:
        err("--company-id is required")

    sup_t = Table("supplier")
    q = (Q.from_(sup_t).select(sup_t.star)
         .where((sup_t.id == P())
                | ((sup_t.name == P()) & (sup_t.company_id == P()))))
    supplier = conn.execute(q.get_sql(),
                            (args.supplier_id, args.supplier_id,
                             args.company_id)).fetchone()
    if not supplier:
        err(f"Supplier {args.supplier_id} not found")
    args.supplier_id = supplier["id"]  # normalize to id
    if supplier["status"] != "active":
        err(f"Supplier {supplier['name']} is {supplier['status']}")

    company_t = Table("company")
    q = Q.from_(company_t).select(company_t.id).where(company_t.id == P())
    if not conn.execute(q.get_sql(), (args.company_id,)).fetchone():
        err(f"Company {args.company_id} not found")
    if supplier["company_id"] != args.company_id:
        err(f"Supplier {args.supplier_id} belongs to another company")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    po_id = str(uuid.uuid4())
    posting_date = args.posting_date or _today()
    total_amount = Decimal("0")

    # Validate items and compute totals
    item_rows = []
    line_discounts = []
    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        qty = to_decimal(item.get("qty", "0"))
        if qty <= 0:
            err(f"Item {i}: qty must be > 0")
        rate = to_decimal(item.get("rate", "0"))
        if rate <= 0:
            err(f"Item {i}: rate must be > 0")

        amount = round_currency(qty * rate)
        stored_pct, net_amount, line_discount = _order_line_net(i, amount, item)
        total_amount += net_amount
        line_discounts.append(str(line_discount))

        item_rows.append((
            str(uuid.uuid4()), po_id, item_id, str(round_currency(qty)),
            item.get("uom"), str(round_currency(rate)), str(amount),
            str(stored_pct), str(net_amount),
            item.get("warehouse_id"), item.get("required_date"),
        ))

    # Calculate tax
    tax_amount, tax_details = _calculate_tax(conn, args.tax_template_id, total_amount)
    grand_total = round_currency(total_amount + tax_amount)

    dims_given = _dimension_input(args)
    dims_obj = dims_given if dims_given is not None else {}
    _validate_dims_before_write(conn, dims_obj)
    dims_text = dimensions_json_text(dims_obj)

    # Insert parent first
    po_t = Table("purchase_order")
    q = (Q.into(po_t)
         .columns("id", "supplier_id", "order_date", "total_amount",
                  "tax_amount", "grand_total", "tax_template_id", "status",
                  "company_id", "dimensions_json")
         .insert(P(), P(), P(), P(), P(), P(), P(), ValueWrapper("draft"), P(), P()))
    conn.execute(q.get_sql(),
        (po_id, args.supplier_id, posting_date,
         str(round_currency(total_amount)), str(round_currency(tax_amount)),
         str(grand_total), args.tax_template_id, args.company_id, dims_text))

    # Insert items
    poi_t = Table("purchase_order_item")
    poi_q = (Q.into(poi_t)
             .columns("id", "purchase_order_id", "item_id", "quantity", "uom",
                      "rate", "amount", "discount_percentage", "net_amount",
                      "warehouse_id", "required_date")
             .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P()))
    poi_sql = poi_q.get_sql()
    for row_params in item_rows:
        conn.execute(poi_sql, row_params)

    audit(conn, "erpclaw-buying", "add-purchase-order", "purchase_order", po_id,
           new_values={"supplier_id": args.supplier_id,
                       "grand_total": str(grand_total),
                       "line_discounts": line_discounts})
    conn.commit()
    ok({"purchase_order_id": po_id,
         "total_amount": str(round_currency(total_amount)),
         "tax_amount": str(round_currency(tax_amount)),
         "grand_total": str(grand_total)})


# ---------------------------------------------------------------------------
# 15. update-purchase-order
# ---------------------------------------------------------------------------

def update_purchase_order(conn, args):
    """Update a draft purchase order's items."""
    if not args.purchase_order_id:
        err("--purchase-order-id is required")

    po_t = Table("purchase_order")
    q = Q.from_(po_t).select(po_t.star).where(po_t.id == P())
    po = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchone()
    if not po:
        err(f"Purchase order {args.purchase_order_id} not found")
    if po["status"] != "draft":
        err(f"Cannot update: PO is '{po['status']}' (must be 'draft')",
             suggestion="Cancel the document first, then make changes.")

    if not args.items:
        err("--items is required for update")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    dims_given = _dimension_input(args)
    old_dims_text = po["dimensions_json"] if po["dimensions_json"] else "{}"
    if dims_given is not None:
        _validate_dims_before_write(conn, dims_given)
        new_dims_text = dimensions_json_text(dims_given)
    else:
        new_dims_text = None

    # Validate every new line before the DELETE so a refusal writes nothing.
    validated = []
    line_discounts = []
    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        qty = to_decimal(item.get("qty", "0"))
        if qty <= 0:
            err(f"Item {i}: qty must be > 0")
        rate = to_decimal(item.get("rate", "0"))
        if rate <= 0:
            err(f"Item {i}: rate must be > 0")
        amount = round_currency(qty * rate)
        stored_pct, net_amount, line_discount = _order_line_net(i, amount, item)
        line_discounts.append(str(line_discount))
        validated.append((item_id, qty, rate, amount, stored_pct, net_amount, item))

    # Delete old items and re-insert
    poi_t = Table("purchase_order_item")
    q = Q.from_(poi_t).delete().where(poi_t.purchase_order_id == P())
    conn.execute(q.get_sql(), (args.purchase_order_id,))

    total_amount = Decimal("0")
    for item_id, qty, rate, amount, stored_pct, net_amount, item in validated:
        total_amount += net_amount

        # raw SQL — reuse same INSERT pattern for PO items
        conn.execute(
            """INSERT INTO purchase_order_item
               (id, purchase_order_id, item_id, quantity, uom, rate, amount,
                discount_percentage, net_amount, warehouse_id, required_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), args.purchase_order_id, item_id,
             str(round_currency(qty)), item.get("uom"),
             str(round_currency(rate)), str(amount),
             str(stored_pct), str(net_amount),
             item.get("warehouse_id"), item.get("required_date")),
        )

    tax_amount, _ = _calculate_tax(conn, po["tax_template_id"], total_amount)
    grand_total = round_currency(total_amount + tax_amount)

    if new_dims_text is not None:
        q = (Q.update(po_t)
             .set(po_t.total_amount, P())
             .set(po_t.tax_amount, P())
             .set(po_t.grand_total, P())
             .set(po_t.dimensions_json, P())
             .set(po_t.updated_at, now())
             .where(po_t.id == P()))
        conn.execute(q.get_sql(),
            (str(round_currency(total_amount)), str(round_currency(tax_amount)),
             str(grand_total), new_dims_text, args.purchase_order_id))
    else:
        q = (Q.update(po_t)
             .set(po_t.total_amount, P())
             .set(po_t.tax_amount, P())
             .set(po_t.grand_total, P())
             .set(po_t.updated_at, now())
             .where(po_t.id == P()))
        conn.execute(q.get_sql(),
            (str(round_currency(total_amount)), str(round_currency(tax_amount)),
             str(grand_total), args.purchase_order_id))

    if new_dims_text is not None:
        audit(conn, "erpclaw-buying", "update-purchase-order", "purchase_order",
               args.purchase_order_id,
               old_values={"dimensions_json": old_dims_text},
               new_values={"grand_total": str(grand_total),
                           "dimensions_json": new_dims_text,
                           "line_discounts": line_discounts})
    else:
        audit(conn, "erpclaw-buying", "update-purchase-order", "purchase_order",
               args.purchase_order_id,
               new_values={"grand_total": str(grand_total),
                           "line_discounts": line_discounts})
    conn.commit()
    ok({"purchase_order_id": args.purchase_order_id,
         "total_amount": str(round_currency(total_amount)),
         "grand_total": str(grand_total)})


# ---------------------------------------------------------------------------
# 16. get-purchase-order
# ---------------------------------------------------------------------------

def get_purchase_order(conn, args):
    """Get PO with items and receipt/billing status."""
    if not args.purchase_order_id:
        err("--purchase-order-id is required")

    po_t = Table("purchase_order")
    q = Q.from_(po_t).select(po_t.star).where(po_t.id == P())
    po = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchone()
    if not po:
        err(f"Purchase order {args.purchase_order_id} not found")

    data = row_to_dict(po)

    # Items with received/invoiced status
    poi = Table("purchase_order_item").as_("poi")
    i_t = Table("item").as_("i")
    q = (Q.from_(poi)
         .left_join(i_t).on(i_t.id == poi.item_id)
         .select(poi.star, i_t.item_code, i_t.item_name)
         .where(poi.purchase_order_id == P())
         .orderby(line_order(poi)))
    items = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchall()
    data["items"] = [row_to_dict(r) for r in items]

    # Linked receipts
    pr_t = Table("purchase_receipt")
    q = (Q.from_(pr_t)
         .select(pr_t.id, pr_t.naming_series, pr_t.status, pr_t.posting_date)
         .where(pr_t.purchase_order_id == P()))
    receipts = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchall()
    data["purchase_receipts"] = [row_to_dict(r) for r in receipts]

    # Linked invoices
    pi_t = Table("purchase_invoice")
    q = (Q.from_(pi_t)
         .select(pi_t.id, pi_t.naming_series, pi_t.status, pi_t.posting_date,
                 pi_t.grand_total, pi_t.outstanding_amount)
         .where(pi_t.purchase_order_id == P()))
    invoices = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchall()
    data["purchase_invoices"] = [row_to_dict(r) for r in invoices]

    ok(data)


# ---------------------------------------------------------------------------
# 17. list-purchase-orders
# ---------------------------------------------------------------------------

def list_purchase_orders(conn, args):
    """List purchase orders."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))
    po = Table("purchase_order").as_("po")
    s = Table("supplier").as_("s")
    params = []

    count_q = Q.from_(po).select(fn.Count("*"))
    data_q = (Q.from_(po)
              .left_join(s).on(s.id == po.supplier_id)
              .select(po.star, s.name.as_("supplier_name")))

    count_q = count_q.where(po.company_id == P())
    data_q = data_q.where(po.company_id == P())
    params.append(company_id)
    if args.supplier_id:
        count_q = count_q.where(po.supplier_id == P())
        data_q = data_q.where(po.supplier_id == P())
        params.append(args.supplier_id)
    if args.po_status:
        count_q = count_q.where(po.status == P())
        data_q = data_q.where(po.status == P())
        params.append(args.po_status)
    if args.from_date:
        count_q = count_q.where(po.order_date >= P())
        data_q = data_q.where(po.order_date >= P())
        params.append(args.from_date)
    if args.to_date:
        count_q = count_q.where(po.order_date <= P())
        data_q = data_q.where(po.order_date <= P())
        params.append(args.to_date)

    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    data_params = params + [limit, offset]

    data_q = (data_q
              .orderby(po.order_date, order=Order.desc)
              .orderby(po.created_at, order=Order.desc)
              .limit(P()).offset(P()))
    rows = conn.execute(data_q.get_sql(), data_params).fetchall()

    ok({"purchase_orders": [row_to_dict(r) for r in rows],
         "total_count": total_count, "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 18. submit-purchase-order
# ---------------------------------------------------------------------------

def submit_purchase_order(conn, args):
    """Submit/confirm a purchase order."""
    if not args.purchase_order_id:
        err("--purchase-order-id is required")

    po_t = Table("purchase_order")
    q = Q.from_(po_t).select(po_t.star).where(po_t.id == P())
    po = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchone()
    if not po:
        err(f"Purchase order {args.purchase_order_id} not found")
    if po["status"] != "draft":
        err(f"Cannot submit: PO is '{po['status']}' (must be 'draft')")

    # Check min order qty warnings (warn, don't block)
    poi_t = Table("purchase_order_item")
    q = Q.from_(poi_t).select(poi_t.star).where(poi_t.purchase_order_id == P())
    po_items = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchall()
    min_qty_warnings = []
    for poi in po_items:
        is_t = Table("item_supplier")
        q = (Q.from_(is_t).select(is_t.min_order_qty)
             .where(is_t.item_id == P())
             .where(is_t.supplier_id == P()))
        is_row = conn.execute(q.get_sql(), (poi["item_id"], po["supplier_id"])).fetchone()
        if is_row and is_row["min_order_qty"]:
            min_qty = to_decimal(str(is_row["min_order_qty"]))
            ordered_qty = to_decimal(str(poi["quantity"]))
            if min_qty > Decimal("0") and ordered_qty < min_qty:
                min_qty_warnings.append({
                    "item_id": poi["item_id"],
                    "ordered_qty": str(ordered_qty),
                    "min_order_qty": str(min_qty),
                })

    naming = get_next_name(conn, "purchase_order", company_id=po["company_id"])

    q = (Q.update(po_t)
         .set(po_t.status, ValueWrapper("confirmed"))
         .set(po_t.naming_series, P())
         .set(po_t.updated_at, now())
         .where(po_t.id == P()))
    conn.execute(q.get_sql(), (naming, args.purchase_order_id))

    audit(conn, "erpclaw-buying", "submit-purchase-order", "purchase_order",
           args.purchase_order_id,
           new_values={"naming_series": naming})
    conn.commit()
    result = {"purchase_order_id": args.purchase_order_id,
              "naming_series": naming, "status": "confirmed"}
    if min_qty_warnings:
        result["warnings"] = min_qty_warnings
    ok(result)


# ---------------------------------------------------------------------------
# 19. cancel-purchase-order
# ---------------------------------------------------------------------------

def cancel_purchase_order(conn, args):
    """Cancel a PO. Only if no linked receipts or invoices."""
    if not args.purchase_order_id:
        err("--purchase-order-id is required")

    po_t = Table("purchase_order")
    q = Q.from_(po_t).select(po_t.star).where(po_t.id == P())
    po = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchone()
    if not po:
        err(f"Purchase order {args.purchase_order_id} not found")
    if po["status"] == "cancelled":
        err("Purchase order is already cancelled")

    # Check for linked receipts
    pr_t = Table("purchase_receipt")
    q = (Q.from_(pr_t)
         .select(fn.Count("*").as_("cnt"))
         .where(pr_t.purchase_order_id == P())
         .where(pr_t.status != P()))
    receipts = conn.execute(q.get_sql(),
        (args.purchase_order_id, "cancelled")).fetchone()
    if receipts["cnt"] > 0:
        err("Cannot cancel: PO has linked purchase receipts")

    # Check for linked invoices
    pi_t = Table("purchase_invoice")
    q = (Q.from_(pi_t)
         .select(fn.Count("*").as_("cnt"))
         .where(pi_t.purchase_order_id == P())
         .where(pi_t.status != P()))
    invoices = conn.execute(q.get_sql(),
        (args.purchase_order_id, "cancelled")).fetchone()
    if invoices["cnt"] > 0:
        err("Cannot cancel: PO has linked purchase invoices")

    q = (Q.update(po_t)
         .set(po_t.status, ValueWrapper("cancelled"))
         .set(po_t.updated_at, now())
         .where(po_t.id == P()))
    conn.execute(q.get_sql(), (args.purchase_order_id,))

    audit(conn, "erpclaw-buying", "cancel-purchase-order", "purchase_order",
           args.purchase_order_id)
    conn.commit()
    ok({"purchase_order_id": args.purchase_order_id, "status": "cancelled"})


# ---------------------------------------------------------------------------
# 20. create-purchase-receipt
# ---------------------------------------------------------------------------

def create_purchase_receipt(conn, args):
    """Create a purchase receipt (GRN) from a PO.

    Subcontracting integration (Wave 2 S5, §Decision 6 single-post): when this
    receipt is for a subcontracting order (--subcontracting-order-id), buying
    DEFERS the finished-goods receipt bookkeeping ENTIRELY to manufacturing's
    receive-subcontracted-items, which owns the FG cost roll-up (raw + subcontract
    charge), the single FG SLE/GL, and the subcontract-charge invoice. Buying does
    NOT also post the FG receipt — both posting would double the SLE and GL. The
    subcontract path is the sole writer of that receipt event.
    """
    subcontracting_order_id = getattr(args, "subcontracting_order_id", None)
    if subcontracting_order_id:
        if not args.received_qty:
            err("--received-qty is required when receiving against a "
                "subcontracting order")
        # Pure delegation: post NOTHING here; the subcontract path owns the GL/SLE.
        from erpclaw_lib.cross_skill import call_skill_action, CrossSkillError
        deleg_args = {
            "--order": subcontracting_order_id,
            "--received-qty": args.received_qty,
        }
        if args.posting_date:
            deleg_args["--posting-date"] = args.posting_date
        sc_rate = getattr(args, "subcontract_charge_rate", None)
        if sc_rate is not None:
            deleg_args["--subcontract-charge-rate"] = sc_rate
        try:
            resp = call_skill_action(
                "erpclaw-manufacturing", "receive-subcontracted-items",
                args=deleg_args,
                db_path=args.db_path,
            )
        except CrossSkillError as e:
            err(f"Subcontracting receipt failed: {e}")
        # Surface the manufacturing result; mark it as delegated so callers know
        # buying posted no second receipt.
        resp["delegated_to"] = "receive-subcontracted-items"
        resp["subcontracting_order_id"] = subcontracting_order_id
        ok(resp)
        return

    if not args.purchase_order_id:
        err("--purchase-order-id is required")

    po_t = Table("purchase_order")
    q = Q.from_(po_t).select(po_t.star).where(po_t.id == P())
    po = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchone()
    if not po:
        err(f"Purchase order {args.purchase_order_id} not found")
    if po["status"] == "closed":
        err("Cannot create receipt: purchase order is closed")
    if po["status"] not in ("confirmed", "partially_received"):
        err(f"Cannot create receipt: PO status is '{po['status']}' "
             f"(must be 'confirmed' or 'partially_received')")

    posting_date = args.posting_date or _today()
    pr_id = str(uuid.uuid4())

    # Determine items: partial (from --items) or full (all PO items)
    items_arg = _parse_json_arg(args.items, "items") if args.items else None

    poi_t = Table("purchase_order_item")
    q = (Q.from_(poi_t).select(poi_t.star)
         .where(poi_t.purchase_order_id == P())
         .orderby(line_order(poi_t)))
    po_items = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchall()

    # --- GRN Tolerance: look up company-level receipt_tolerance_pct ---
    company_t = Table("company")
    _co_q = Q.from_(company_t).select(company_t.receipt_tolerance_pct).where(company_t.id == P())
    _co_row = conn.execute(_co_q.get_sql(), (po["company_id"],)).fetchone()
    _tolerance_pct = to_decimal(_co_row["receipt_tolerance_pct"]) if _co_row else Decimal("0")

    total_qty = Decimal("0")
    receipt_items = []
    _disc_intra_qty = {}
    _disc_intra_disc = {}
    _disc_audit = []

    if items_arg:
        # Partial receipt
        for i, item in enumerate(items_arg):
            po_item_id = item.get("purchase_order_item_id")
            if not po_item_id:
                err(f"Item {i}: purchase_order_item_id is required for partial receipt")
            poi_lookup_q = Q.from_(poi_t).select(poi_t.star).where(poi_t.id == P())
            poi = conn.execute(poi_lookup_q.get_sql(), (po_item_id,)).fetchone()
            if not poi:
                err(f"Item {i}: PO item {po_item_id} not found")

            qty = to_decimal(item.get("qty", "0"))
            if qty <= 0:
                err(f"Item {i}: qty must be > 0")

            # Check remaining receivable qty with GRN tolerance
            ordered = to_decimal(poi["quantity"])
            received = to_decimal(poi["received_qty"])
            remaining = ordered - received
            max_allowed = round_currency(
                remaining * (Decimal("1") + _tolerance_pct / Decimal("100"))
            )
            if qty > max_allowed:
                if _tolerance_pct > 0:
                    err(f"Item {i}: qty {qty} exceeds allowed receivable "
                        f"{max_allowed} (remaining {remaining} + "
                        f"{_tolerance_pct}% tolerance)")
                else:
                    err(f"Item {i}: qty {qty} exceeds remaining receivable {remaining}")

            rate = to_decimal(poi["rate"])
            amount = round_currency(qty * rate)
            total_qty += qty

            _share = _preview_line_discount(
                conn, poi, qty, _disc_intra_qty, _disc_intra_disc)
            _line_id = str(uuid.uuid4())
            _disc_audit.append({"line": _line_id,
                                "discount_amount": _discount_text(_share),
                                "net": str(round_currency(amount - _share))})
            receipt_items.append((
                _line_id, pr_id, poi["item_id"],
                str(round_currency(qty)), poi["uom"], po_item_id,
                item.get("warehouse_id") or poi["warehouse_id"],
                item.get("batch_id"), item.get("serial_numbers"),
                str(round_currency(rate)), str(amount),
                _discount_text(_share),
            ))
    else:
        # Full receipt: copy all unreceived PO items
        for poi_row in po_items:
            poi = row_to_dict(poi_row)
            ordered = to_decimal(poi["quantity"])
            received = to_decimal(poi["received_qty"])
            remaining = ordered - received
            if remaining <= 0:
                continue

            rate = to_decimal(poi["rate"])
            amount = round_currency(remaining * rate)
            total_qty += remaining

            _share = _preview_line_discount(
                conn, poi, remaining, _disc_intra_qty, _disc_intra_disc)
            _line_id = str(uuid.uuid4())
            _disc_audit.append({"line": _line_id,
                                "discount_amount": _discount_text(_share),
                                "net": str(round_currency(amount - _share))})
            receipt_items.append((
                _line_id, pr_id, poi["item_id"],
                str(round_currency(remaining)), poi["uom"], poi["id"],
                poi["warehouse_id"], None, None,
                str(round_currency(rate)), str(amount),
                _discount_text(_share),
            ))

    if not receipt_items:
        err("No items to receive (all PO items already fully received)")

    dims_given = _dimension_input(args)
    if dims_given is not None:
        dims_obj = dims_given
        dims_text = dimensions_json_text(dims_given)
    else:
        parent_text = po["dimensions_json"] if po["dimensions_json"] else "{}"
        dims_text = parent_text
        try:
            dims_obj = json.loads(parent_text)
        except (ValueError, TypeError):
            dims_obj = {}
        if not isinstance(dims_obj, dict):
            dims_obj = {}
    _validate_dims_before_write(conn, dims_obj)

    # Insert parent first
    pr_t = Table("purchase_receipt")
    q = (Q.into(pr_t)
         .columns("id", "supplier_id", "posting_date", "purchase_order_id",
                  "status", "total_qty", "company_id", "dimensions_json")
         .insert(P(), P(), P(), P(), ValueWrapper("draft"), P(), P(), P()))
    conn.execute(q.get_sql(),
        (pr_id, po["supplier_id"], posting_date, args.purchase_order_id,
         str(round_currency(total_qty)), po["company_id"], dims_text))

    # Insert items
    pri_t = Table("purchase_receipt_item")
    pri_q = (Q.into(pri_t)
             .columns("id", "purchase_receipt_id", "item_id", "quantity",
                      "uom", "purchase_order_item_id", "warehouse_id",
                      "batch_id", "serial_numbers", "rate", "amount",
                      "discount_amount")
             .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(),
                     P()))
    pri_sql = pri_q.get_sql()
    for row_params in receipt_items:
        conn.execute(pri_sql, row_params)

    audit(conn, "erpclaw-buying", "create-purchase-receipt", "purchase_receipt", pr_id,
           new_values={"purchase_order_id": args.purchase_order_id,
                       "item_count": len(receipt_items),
                       "line_discounts": _disc_audit})
    conn.commit()
    ok({"purchase_receipt_id": pr_id, "total_qty": str(round_currency(total_qty)),
         "item_count": len(receipt_items)})


# ---------------------------------------------------------------------------
# 21. get-purchase-receipt
# ---------------------------------------------------------------------------

def get_purchase_receipt(conn, args):
    """Get a purchase receipt with items."""
    if not args.purchase_receipt_id:
        err("--purchase-receipt-id is required")

    pr_t = Table("purchase_receipt")
    q = Q.from_(pr_t).select(pr_t.star).where(pr_t.id == P())
    pr = conn.execute(q.get_sql(), (args.purchase_receipt_id,)).fetchone()
    if not pr:
        err(f"Purchase receipt {args.purchase_receipt_id} not found")

    data = row_to_dict(pr)

    pri = Table("purchase_receipt_item").as_("pri")
    i_t = Table("item").as_("i")
    q = (Q.from_(pri)
         .left_join(i_t).on(i_t.id == pri.item_id)
         .select(pri.star, i_t.item_code, i_t.item_name)
         .where(pri.purchase_receipt_id == P())
         .orderby(line_order(pri)))
    items = conn.execute(q.get_sql(), (args.purchase_receipt_id,)).fetchall()
    data["items"] = [row_to_dict(r) for r in items]

    ok(data)


# ---------------------------------------------------------------------------
# 22. list-purchase-receipts
# ---------------------------------------------------------------------------

def list_purchase_receipts(conn, args):
    """List purchase receipts."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))
    pr = Table("purchase_receipt").as_("pr")
    s = Table("supplier").as_("s")
    params = []

    count_q = Q.from_(pr).select(fn.Count("*"))
    data_q = (Q.from_(pr)
              .left_join(s).on(s.id == pr.supplier_id)
              .select(pr.star, s.name.as_("supplier_name")))

    count_q = count_q.where(pr.company_id == P())
    data_q = data_q.where(pr.company_id == P())
    params.append(company_id)
    if args.supplier_id:
        count_q = count_q.where(pr.supplier_id == P())
        data_q = data_q.where(pr.supplier_id == P())
        params.append(args.supplier_id)
    if args.pr_status:
        count_q = count_q.where(pr.status == P())
        data_q = data_q.where(pr.status == P())
        params.append(args.pr_status)

    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    data_params = params + [limit, offset]

    data_q = (data_q
              .orderby(pr.posting_date, order=Order.desc)
              .orderby(pr.created_at, order=Order.desc)
              .limit(P()).offset(P()))
    rows = conn.execute(data_q.get_sql(), data_params).fetchall()

    ok({"purchase_receipts": [row_to_dict(r) for r in rows],
         "total_count": total_count, "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 23. submit-purchase-receipt
# ---------------------------------------------------------------------------

def submit_purchase_receipt(conn, args):
    """Submit a GRN: create SLE + perpetual inventory GL."""
    if not args.purchase_receipt_id:
        err("--purchase-receipt-id is required")

    pr_t = Table("purchase_receipt")
    q = Q.from_(pr_t).select(pr_t.star).where(pr_t.id == P())
    pr = conn.execute(q.get_sql(), (args.purchase_receipt_id,)).fetchone()
    if not pr:
        err(f"Purchase receipt {args.purchase_receipt_id} not found")
    if pr["status"] != "draft":
        err(f"Cannot submit: receipt is '{pr['status']}' (must be 'draft')")

    pr_dict = row_to_dict(pr)
    company_id = pr_dict["company_id"]
    posting_date = pr_dict["posting_date"]

    # Verify linked PO is confirmed (if exists)
    if pr_dict.get("purchase_order_id"):
        po_t = Table("purchase_order")
        q = Q.from_(po_t).select(po_t.status).where(po_t.id == P())
        po = conn.execute(q.get_sql(),
                          (pr_dict["purchase_order_id"],)).fetchone()
        if po and po["status"] not in ("confirmed", "partially_received",
                                        "partially_invoiced"):
            err(f"Linked PO status is '{po['status']}' -- must be confirmed")

    pri_t = Table("purchase_receipt_item")
    q = (Q.from_(pri_t).select(pri_t.star)
         .where(pri_t.purchase_receipt_id == P())
         .orderby(line_order(pri_t)))
    items = conn.execute(q.get_sql(), (args.purchase_receipt_id,)).fetchall()
    if not items:
        err("Purchase receipt has no items")

    fiscal_year = get_fiscal_year(conn, posting_date, company_id=company_id)
    cost_center_id = _get_cost_center(conn, company_id)
    naming = get_next_name(conn, "purchase_receipt", company_id=company_id)

    take_chain_heads(conn, [company_id])

    # Update PO received_qty (moved ahead of the SLE build so the head take
    # precedes every prior-sum read below)
    for item_row in items:
        item = row_to_dict(item_row)
        if item.get("purchase_order_item_id"):
            # raw SQL — CAST arithmetic expression not expressible in PyPika
            conn.execute(
                """UPDATE purchase_order_item
                   SET received_qty = CAST(
                       CAST(received_qty AS NUMERIC) + CAST(? AS NUMERIC) AS TEXT)
                   WHERE id = ?""",
                (item["quantity"], item["purchase_order_item_id"]),
            )

    # Re-derive every order-linked line's discount share (rule of record).
    # A changed value is written to the draft line; lines are re-read below.
    _poi_cache = {}
    _intra_qty = {}
    _intra_disc = {}
    _rederived = []
    _rederived_old = {}
    _completing_pois = {}
    poi_t = Table("purchase_order_item")
    for item_row in items:
        item = row_to_dict(item_row)
        _po_item_id = item.get("purchase_order_item_id")
        if not _po_item_id:
            continue
        if _po_item_id not in _poi_cache:
            _pq = (Q.from_(poi_t).select(poi_t.star)
                   .where(poi_t.id == P()))
            _poi_cache[_po_item_id] = conn.execute(
                _pq.get_sql(), (_po_item_id,)).fetchone()
        _poi = _poi_cache[_po_item_id]
        if _poi is None:
            continue
        _Q = to_decimal(_poi["quantity"])
        _D = round_currency(
            to_decimal(_poi["amount"]) - to_decimal(_poi["net_amount"]))
        _q = to_decimal(item["quantity"])
        _sub_qty, _sub_disc = _receipt_prior_sums(conn, _po_item_id)
        _prior_qty = _sub_qty + _intra_qty.get(_po_item_id, Decimal("0"))
        _prior_disc = _sub_disc + _intra_disc.get(_po_item_id, Decimal("0"))
        _covered = min(_q, max(_Q - _prior_qty, Decimal("0")))
        if _covered > 0 and _prior_qty + _covered == _Q:
            _completing_pois[_po_item_id] = (_Q, _D)
        _new_val = discount_share(_D, _Q, _prior_qty, _prior_disc, _q)
        # Legacy overflow: a retroactive order discount (set after earlier
        # receipts posted) that swallows this line refuses here, after the
        # received_qty UPDATE above and before anything posts, so a rollback
        # leaves the order line exactly as it was.
        _line_amount = to_decimal(item.get("amount") or "0")
        if _new_val > 0 and _new_val >= _line_amount:
            err(f"Item {item.get('item_id')}: the remaining order discount {_new_val:.2f} "
                f"is not less than this line's amount {_line_amount:.2f}; cancel and "
                f"re-receive the earlier receipts of this order line so the discount "
                f"is spread, then retry")
        _new_text = _discount_text(_new_val)
        _old_text = item.get("discount_amount") or "0"
        _intra_qty[_po_item_id] = (_intra_qty.get(_po_item_id, Decimal("0"))
                                   + _q)
        _intra_disc[_po_item_id] = (_intra_disc.get(_po_item_id, Decimal("0"))
                                    + _new_val)
        if _new_text != _old_text:
            _uq = (Q.update(pri_t)
                   .set(pri_t.discount_amount, P())
                   .where(pri_t.id == P()))
            conn.execute(_uq.get_sql(), (_new_text, item["id"]))
            _rederived.append({"line": item["id"],
                               "old": _old_text, "new": _new_text})
            _rederived_old[item["id"]] = _old_text
    _true_up = False
    for _po_item_id, (_Q, _D) in _completing_pois.items():
        _sib_q = (Q.from_(pri_t)
                  .join(pr_t).on(pr_t.id == pri_t.purchase_receipt_id)
                  .select(pri_t.quantity, pri_t.discount_amount)
                  .where(pri_t.purchase_order_item_id == P())
                  .where(pr_t.status == ValueWrapper("submitted"))
                  .orderby(pr_t.created_at)
                  .orderby(line_order(pri_t)))
        _sibs = conn.execute(_sib_q.get_sql(), (_po_item_id,)).fetchall()
        _run_qty = Decimal("0")
        _run_disc = Decimal("0")
        for _sib in _sibs:
            _sq = to_decimal(_sib["quantity"])
            _sd_text = _sib["discount_amount"] or "0"
            if _sd_text == "0":
                if discount_share(_D, _Q, _run_qty, _run_disc, _sq) != 0:
                    _true_up = True
                    break
            _run_qty += _sq
            _run_disc += to_decimal(_sd_text)
        if _true_up:
            break
    q = (Q.from_(pri_t).select(pri_t.star)
         .where(pri_t.purchase_receipt_id == P())
         .orderby(line_order(pri_t)))
    items = conn.execute(q.get_sql(), (args.purchase_receipt_id,)).fetchall()
    if not items:
        err("Purchase receipt has no items")

    # A discounted FIFO line whose net no per-unit rate can hold refuses
    # before anything posts.
    for item_row in items:
        item = row_to_dict(item_row)
        _fifo_disc = to_decimal(item.get("discount_amount") or "0")
        if _fifo_disc != 0:
            _fifo_msg = check_fifo_discounted_net(
                conn, item.get("item_id"),
                round_currency(to_decimal(item.get("amount") or "0") - _fifo_disc),
                item.get("quantity"))
            if _fifo_msg is not None:
                err(_fifo_msg)

    # Build SLE entries (positive qty into warehouse)
    sle_entries = []
    for item_row in items:
        item = row_to_dict(item_row)
        qty = to_decimal(item["quantity"])
        rate = to_decimal(item["rate"])
        warehouse_id = item.get("warehouse_id")
        if not warehouse_id:
            # Fallback to company default warehouse
            company_t = Table("company")
            co_q = Q.from_(company_t).select(company_t.default_warehouse_id).where(company_t.id == P())
            co = conn.execute(co_q.get_sql(), (company_id,)).fetchone()
            warehouse_id = co["default_warehouse_id"] if co else None
        if not warehouse_id:
            err(f"No warehouse specified for item {item['item_id']} and no company default")

        _sle_entry = {
            "item_id": item["item_id"],
            "warehouse_id": warehouse_id,
            "actual_qty": str(round_currency(qty)),
            "incoming_rate": str(round_currency(rate)),
            "batch_id": item.get("batch_id"),
            "serial_number": item.get("serial_numbers"),
            "fiscal_year": fiscal_year,
            # FINDING-010 / ADR-0014: GRN is a true external receipt — it must carry
            # a positive cost (the PO line rate), never silently book inventory at $0.
            "require_rate": True,
        }
        # A discounted line values stock at exactly its net.
        _line_disc = to_decimal(item.get("discount_amount") or "0")
        if _line_disc != 0:
            _sle_entry["incoming_value"] = str(
                to_decimal(item.get("amount") or "0") - _line_disc)
        sle_entries.append(_sle_entry)

    # Insert SLE
    try:
        sle_ids = insert_sle_entries(
            conn, sle_entries,
            voucher_type="purchase_receipt",
            voucher_id=args.purchase_receipt_id,
            posting_date=posting_date,
            company_id=company_id,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-buying] {e}\n")
        err(f"SLE posting failed: {e}")

    # Build perpetual inventory GL: DR Stock In Hand / CR Stock Received Not Billed
    sle_t = Table("stock_ledger_entry")
    q = (Q.from_(sle_t).select(sle_t.star)
         .where(sle_t.voucher_type == ValueWrapper("purchase_receipt"))
         .where(sle_t.voucher_id == P())
         .where(sle_t.is_cancelled == 0))
    sle_rows = conn.execute(q.get_sql(), (args.purchase_receipt_id,)).fetchall()
    sle_dicts = [row_to_dict(r) for r in sle_rows]

    _pr_dims_raw = pr_dict.get("dimensions_json") or "{}"
    try:
        _pr_dims_obj = json.loads(_pr_dims_raw)
    except (ValueError, TypeError):
        _pr_dims_obj = {}
    if not isinstance(_pr_dims_obj, dict):
        _pr_dims_obj = {}
    if _pr_dims_obj.get("cost_center"):
        cost_center_id = _pr_dims_obj["cost_center"]

    try:
        if _pr_dims_obj:
            gl_entries = create_perpetual_inventory_gl(
                conn, sle_dicts,
                voucher_type="purchase_receipt",
                voucher_id=args.purchase_receipt_id,
                posting_date=posting_date,
                company_id=company_id,
                cost_center_id=cost_center_id,
                dimensions=dict(_pr_dims_obj),
            )
        else:
            gl_entries = create_perpetual_inventory_gl(
                conn, sle_dicts,
                voucher_type="purchase_receipt",
                voucher_id=args.purchase_receipt_id,
                posting_date=posting_date,
                company_id=company_id,
                cost_center_id=cost_center_id,
            )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-buying] {e}\n")
        err(f"GL posting failed: {e}")

    gl_ids = []
    if gl_entries:
        for gle in gl_entries:
            gle["fiscal_year"] = fiscal_year
        try:
            gl_ids = insert_gl_entries(
                conn, gl_entries,
                voucher_type="purchase_receipt",
                voucher_id=args.purchase_receipt_id,
                posting_date=posting_date,
                company_id=company_id,
                remarks=f"Purchase Receipt {naming}",
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-buying] {e}\n")
            err(f"GL posting failed: {e}")

    # Update PO status
    if pr_dict.get("purchase_order_id"):
        _update_po_receipt_status(conn, pr_dict["purchase_order_id"])

    # Update receipt status
    q = (Q.update(pr_t)
         .set(pr_t.status, ValueWrapper("submitted"))
         .set(pr_t.naming_series, P())
         .set(pr_t.updated_at, now())
         .where(pr_t.id == P()))
    conn.execute(q.get_sql(), (naming, args.purchase_receipt_id))

    _submit_line_discounts = []
    for item_row in items:
        item = row_to_dict(item_row)
        _d = item.get("discount_amount") or "0"
        _a = item.get("amount") or "0"
        _submit_line_discounts.append({
            "line": item["id"], "discount_amount": _d,
            "net": str(round_currency(to_decimal(_a) - to_decimal(_d)))})
    _submit_new = {"naming_series": naming,
                   "sle_count": len(sle_ids), "gl_count": len(gl_ids),
                   "line_discounts": _submit_line_discounts}
    if _rederived:
        _submit_new["discount_rederived"] = _rederived
    if _rederived:
        audit(conn, "erpclaw-buying", "submit-purchase-receipt",
               "purchase_receipt", args.purchase_receipt_id,
               old_values={"discount_amount": _rederived_old},
               new_values=_submit_new)
    else:
        audit(conn, "erpclaw-buying", "submit-purchase-receipt",
               "purchase_receipt", args.purchase_receipt_id,
               new_values=_submit_new)
    conn.commit()
    _resp = {"purchase_receipt_id": args.purchase_receipt_id,
             "naming_series": naming, "status": "submitted",
             "sle_entries_created": len(sle_ids),
             "gl_entries_created": len(gl_ids)}
    if _rederived:
        _resp["discount_rederived"] = _rederived
    if _true_up:
        _resp["discount_true_up"] = True
    ok(_resp)


def _update_po_receipt_status(conn, purchase_order_id):
    """Update PO per_received and status based on received quantities."""
    poi_t = Table("purchase_order_item")
    q = (Q.from_(poi_t)
         .select(poi_t.quantity, poi_t.received_qty)
         .where(poi_t.purchase_order_id == P()))
    po_items = conn.execute(q.get_sql(), (purchase_order_id,)).fetchall()

    total_ordered = Decimal("0")
    total_received = Decimal("0")
    for poi in po_items:
        total_ordered += to_decimal(poi["quantity"])
        total_received += to_decimal(poi["received_qty"])

    if total_ordered > 0:
        per_received = round_currency(total_received / total_ordered * Decimal("100"))
    else:
        per_received = Decimal("0")

    if per_received >= Decimal("100"):
        new_status = "fully_received"
    elif per_received > Decimal("0"):
        new_status = "partially_received"
    else:
        return  # No change

    po_t = Table("purchase_order")
    q = (Q.update(po_t)
         .set(po_t.per_received, P())
         .set(po_t.status, P())
         .set(po_t.updated_at, now())
         .where(po_t.id == P()))
    conn.execute(q.get_sql(), (str(per_received), new_status, purchase_order_id))


# ---------------------------------------------------------------------------
# 24. cancel-purchase-receipt
# ---------------------------------------------------------------------------

def cancel_purchase_receipt(conn, args):
    """Cancel a submitted GRN: reverse SLE + GL."""
    if not args.purchase_receipt_id:
        err("--purchase-receipt-id is required")

    pr_t = Table("purchase_receipt")
    q = Q.from_(pr_t).select(pr_t.star).where(pr_t.id == P())
    pr = conn.execute(q.get_sql(), (args.purchase_receipt_id,)).fetchone()
    if not pr:
        err(f"Purchase receipt {args.purchase_receipt_id} not found")
    if pr["status"] != "submitted":
        err(f"Cannot cancel: receipt is '{pr['status']}' (must be 'submitted')")

    pr_dict = row_to_dict(pr)
    posting_date = pr_dict["posting_date"]

    # Reverse SLE
    try:
        reversal_sle_ids = reverse_sle_entries(
            conn,
            voucher_type="purchase_receipt",
            voucher_id=args.purchase_receipt_id,
            posting_date=posting_date,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-buying] {e}\n")
        err(f"SLE reversal failed: {e}")

    # Reverse GL
    try:
        reversal_gl_ids = reverse_gl_entries(
            conn,
            voucher_type="purchase_receipt",
            voucher_id=args.purchase_receipt_id,
            posting_date=posting_date,
        )
    except ValueError:
        reversal_gl_ids = []

    # Reverse PO received_qty
    pri_t = Table("purchase_receipt_item")
    q = Q.from_(pri_t).select(pri_t.star).where(pri_t.purchase_receipt_id == P())
    items = conn.execute(q.get_sql(), (args.purchase_receipt_id,)).fetchall()
    for item_row in items:
        item = row_to_dict(item_row)
        if item.get("purchase_order_item_id"):
            # raw SQL — CAST+MAX arithmetic expression not expressible in PyPika
            conn.execute(
                f"""UPDATE purchase_order_item
                   SET received_qty = CAST(
                       {scalar_max("0", "CAST(received_qty AS NUMERIC) - CAST(? AS NUMERIC)")} AS TEXT)
                   WHERE id = ?""",
                (item["quantity"], item["purchase_order_item_id"]),
            )

    # Update PO status back
    if pr_dict.get("purchase_order_id"):
        _update_po_receipt_status(conn, pr_dict["purchase_order_id"])
        # If all received is now 0, set back to confirmed
        poi_t = Table("purchase_order_item")
        q = (Q.from_(poi_t).select(poi_t.received_qty)
             .where(poi_t.purchase_order_id == P()))
        po_items = conn.execute(q.get_sql(),
            (pr_dict["purchase_order_id"],)).fetchall()
        all_zero = all(to_decimal(p["received_qty"]) <= 0 for p in po_items)
        if all_zero:
            po_t = Table("purchase_order")
            q = (Q.update(po_t)
                 .set(po_t.status, ValueWrapper("confirmed"))
                 .set(po_t.per_received, ValueWrapper("0"))
                 .set(po_t.updated_at, now())
                 .where(po_t.id == P()))
            conn.execute(q.get_sql(), (pr_dict["purchase_order_id"],))

    q = (Q.update(pr_t)
         .set(pr_t.status, ValueWrapper("cancelled"))
         .set(pr_t.updated_at, now())
         .where(pr_t.id == P()))
    conn.execute(q.get_sql(), (args.purchase_receipt_id,))

    audit(conn, "erpclaw-buying", "cancel-purchase-receipt", "purchase_receipt",
           args.purchase_receipt_id,
           new_values={"reversed": True})
    conn.commit()
    ok({"purchase_receipt_id": args.purchase_receipt_id,
         "status": "cancelled",
         "sle_reversals": len(reversal_sle_ids),
         "gl_reversals": len(reversal_gl_ids)})


def _document_currency(conn, supplier_row, company_id):
    if supplier_row is not None:
        try:
            raw = supplier_row["default_currency"]
        except (KeyError, IndexError, TypeError, ValueError):
            raw = None
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip()
    co_t = Table("company")
    coq = Q.from_(co_t).select(co_t.default_currency).where(co_t.id == P())
    row = conn.execute(coq.get_sql(), (company_id,)).fetchone()
    if row is not None:
        try:
            craw = row["default_currency"]
        except (KeyError, IndexError, TypeError, ValueError):
            craw = None
        if craw is not None and str(craw).strip() != "":
            return str(craw).strip()
    return "USD"


# ---------------------------------------------------------------------------
# 25. create-purchase-invoice
# ---------------------------------------------------------------------------

def _capture_bytes(args):
    """Read one immutable bounded local capture, without following a symlink."""
    path = getattr(args, "capture_file", None)
    if not isinstance(path, str) or not path or len(path) > 1000:
        err("--capture-file must name a local regular file")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 10 * 1024 * 1024:
                err("Capture must be a nonempty regular file of at most 10 MiB")
            content = source.read(10 * 1024 * 1024 + 1)
    except OSError:
        err("Capture file cannot be read as a local regular file")
    if not content or len(content) > 10 * 1024 * 1024:
        err("Capture must contain at most 10 MiB")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(content) < 33 or content[12:16] != b"IHDR":
            err("Capture PNG header is invalid")
        width, height = struct.unpack(">II", content[16:24])
        if not 0 < width <= 10000 or not 0 < height <= 10000 or width * height > 20000000:
            err("Capture PNG must contain at most 20 million pixels")
        kind = "png"
    elif content.startswith(b"%PDF-"):
        kind = "pdf"
    else:
        err("Capture supports PNG images and text-layer PDF files only")
    return content, kind, hashlib.sha256(content).hexdigest()


def _capture_company(conn, args):
    company_id = getattr(args, "company_id", None)
    company = Table("company")
    query = Q.from_(company).select(company.id).where(company.id == P())
    if not company_id or conn.execute(query.get_sql(), (company_id,)).fetchone() is None:
        err("--company-id must identify an existing company")


def capture_vendor_bill(conn, args):
    """Return local untrusted extraction for review, without database writes."""
    _capture_company(conn, args)
    content, kind, digest = _capture_bytes(args)
    tool = "tesseract" if kind == "png" else "pdftotext"
    executable = shutil.which(tool, path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if executable is None:
        err(f"Local capture requires {tool}; no extraction or draft was created")
    with tempfile.TemporaryDirectory(prefix="erpclaw-bill-") as folder:
        source_path = os.path.join(folder, "input." + kind)
        with open(source_path, "xb") as target:
            target.write(content)
        output_base = os.path.join(folder, "extracted")
        output_path = output_base + ".txt"
        command = ([executable, source_path, output_base, "-l", "eng", "--psm", "6"]
                   if kind == "png" else
                   [executable, "-layout", "-enc", "UTF-8", source_path, output_path])
        try:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "OMP_THREAD_LIMIT": "1",
                     "TMPDIR": folder})
            if result.returncode != 0:
                err("Local extraction failed; no draft was created")
            with open(output_path, "rb") as output:
                extracted = output.read(65537)
            if len(extracted) > 65536:
                err("Extracted text exceeds 64 KiB; split the capture before retrying")
            text = extracted.decode("utf-8")
        except subprocess.TimeoutExpired:
            err("Local extraction exceeded 30 seconds; no draft was created")
        except (OSError, UnicodeError):
            err("Local extraction did not produce readable UTF-8 text")
    if not text.strip():
        err("No text extracted; PDF capture requires a text layer")
    ok({"company_id": args.company_id, "capture_sha256": digest,
        "capture_bytes": len(content), "format": kind, "tool": tool,
        "text": text, "untrusted": True, "review_required": True,
        "draft_created": False})


def add_captured_vendor_bill(conn, args):
    """Save separately reviewed fields against the exact local capture bytes."""
    _capture_company(conn, args)
    _, _, digest = _capture_bytes(args)
    expected = getattr(args, "capture_sha256", None)
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        err("--capture-sha256 must be the reviewed capture's lowercase SHA-256")
    if expected != digest:
        err("Capture changed since review; extract and review the current file again")
    raw = getattr(args, "bill_json", None)
    if not isinstance(raw, str) or not raw or len(raw) > 5000:
        err("--bill-json must contain separately reviewed fields of at most 5000 characters")
    try:
        bill = json.loads(raw)
    except ValueError:
        err("Invalid JSON for --bill-json")
    if not isinstance(bill, dict) or "source_message_id" in bill:
        err("Captured bill fields must be an object without source_message_id")
    bill["source_message_id"] = "capture:" + digest
    reviewed = argparse.Namespace(
        bill_json=json.dumps(bill), company_id=args.company_id,
        _intake_source_kind="local-capture", _intake_capture_sha256=digest)
    add_vendor_bill_intake(conn, reviewed)


def add_vendor_bill_intake(conn, args):
    """Turn reviewed, structured email fields into a draft, never a posting."""
    raw = getattr(args, "bill_json", None)
    if not isinstance(raw, str) or not raw or len(raw) > 5000:
        err("--bill-json must be a JSON object of at most 5000 characters")
    try:
        bill = json.loads(raw)
    except (ValueError, TypeError):
        err("Invalid JSON for --bill-json")
    required = {"source_message_id", "supplier_id", "company_id", "posting_date", "items"}
    if not isinstance(bill, dict) or not required.issubset(bill):
        err("--bill-json requires source_message_id, supplier_id, company_id, posting_date and items")
    if set(bill) - (required | {"due_date"}):
        err("--bill-json contains unsupported fields; intake saves drafts only")
    for key in ("source_message_id", "supplier_id", "company_id"):
        value = bill[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            err(f"{key} must be nonempty text of at most 200 characters")
    if not getattr(args, "company_id", None) or args.company_id != bill["company_id"]:
        err("--company-id must match the reviewed bill's company_id")
    for key in ("posting_date", "due_date"):
        value = bill.get(key)
        if key == "due_date" and value is None:
            continue
        try:
            if not isinstance(value, str) or date_type.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except ValueError:
            err(f"{key} must be an ISO date (YYYY-MM-DD)")
    if bill.get("due_date") and bill["due_date"] < bill["posting_date"]:
        err("due_date must not precede posting_date")
    items = bill["items"]
    if not isinstance(items, list) or not items or len(items) > 100:
        err("items must be a nonempty array of at most 100 lines")
    normalized = []
    supplier_t = Table("supplier")
    supplier_query = (Q.from_(supplier_t).select(supplier_t.status)
                      .where(supplier_t.id == P()))
    supplier = conn.execute(supplier_query.get_sql(), (bill["supplier_id"],)).fetchone()
    if supplier is None or supplier["status"] != "active":
        err("supplier_id must identify an active supplier")
    item_t = Table("item")
    for index, line in enumerate(items):
        if not isinstance(line, dict) or set(line) != {"item_id", "qty", "rate"}:
            err(f"Item {index}: requires only item_id, qty and rate")
        item_id = line["item_id"]
        if not isinstance(item_id, str) or not item_id or len(item_id) > 200:
            err(f"Item {index}: item_id must be nonempty text")
        query = Q.from_(item_t).select(item_t.status).where(item_t.id == P())
        item = conn.execute(query.get_sql(), (item_id,)).fetchone()
        if item is None or item["status"] == "disabled":
            err(f"Item {index}: item_id must identify an active item")
        for key in ("qty", "rate"):
            value = line[key]
            if (not isinstance(value, str)
                    or re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", value) is None
                    or Decimal(value) <= 0):
                err(f"Item {index}: {key} must be positive Decimal text with at most two decimal places")
        if round_currency(Decimal(line["qty"]) * Decimal(line["rate"])) <= 0:
            err(f"Item {index}: line amount must be at least 0.01")
        normalized.append(dict(line))
    draft_args = argparse.Namespace(
        supplier_id=bill["supplier_id"], company_id=bill["company_id"],
        posting_date=bill["posting_date"], due_date=bill.get("due_date"),
        items=json.dumps(normalized), purchase_order_id=None,
        purchase_receipt_id=None, tax_template_id=None,
        _intake_source_message_id=bill["source_message_id"],
        _intake_source_kind=getattr(args, "_intake_source_kind", "email"),
        _intake_capture_sha256=getattr(args, "_intake_capture_sha256", None),
    )
    create_purchase_invoice(conn, draft_args)


def create_purchase_invoice(conn, args):
    """Create a purchase invoice (from PO, GRN, or standalone)."""
    company_id = args.company_id
    supplier_id = args.supplier_id
    items_arg = _parse_json_arg(args.items, "items") if args.items else None
    posting_date = args.posting_date or _today()
    due_date = args.due_date
    tax_template_id = args.tax_template_id
    po_id = args.purchase_order_id
    pr_id_arg = args.purchase_receipt_id
    update_stock = 1  # Default for US perpetual inventory

    # S3 CWIP hook (AVA-43): a --cwip-asset-id bill is a standalone cost capitalised
    # to construction-in-progress, not a stock receipt. Validate the asset is
    # under_construction up front and force cost-bill semantics; submit routes the
    # expense GL to the asset's CWIP account and records the accumulation in-tx.
    cwip_asset_id = getattr(args, "cwip_asset_id", None)
    if cwip_asset_id:
        if po_id or pr_id_arg:
            err("--cwip-asset-id is for standalone cost bills; it cannot combine with "
                "--purchase-order-id / --purchase-receipt-id (CWIP costs are not stock receipts).")
        try:
            get_under_construction_asset(conn, cwip_asset_id)
        except ValueError as e:
            err(str(e))
        update_stock = 0  # CWIP cost posts to CWIP, no inventory movement

    pi_id = str(uuid.uuid4())
    pi_items = []
    total_amount = Decimal("0")
    _bill_line_discounts = []
    _bill_intra_qty = {}
    _bill_intra_disc = {}

    po_t = Table("purchase_order")
    poi_t = Table("purchase_order_item")
    pr_t_lookup = Table("purchase_receipt")

    if po_id:
        # Create from Purchase Order
        q = Q.from_(po_t).select(po_t.star).where(po_t.id == P())
        po = conn.execute(q.get_sql(), (po_id,)).fetchone()
        if not po:
            err(f"Purchase order {po_id} not found")
        if po["status"] == "closed":
            err("Cannot create invoice: purchase order is closed")
        supplier_id = po["supplier_id"]
        company_id = po["company_id"]
        tax_template_id = tax_template_id or po["tax_template_id"]

        # If PO has receipts, set update_stock=0 (stock already moved)
        q = (Q.from_(pr_t_lookup)
             .select(pr_t_lookup.id)
             .where(pr_t_lookup.purchase_order_id == P())
             .where(pr_t_lookup.status == ValueWrapper("submitted"))
             .limit(1))
        receipt_row = conn.execute(q.get_sql(), (po_id,)).fetchone()
        if receipt_row:
            update_stock = 0
            if not pr_id_arg:
                pr_id_arg = receipt_row["id"]

        q = (Q.from_(poi_t).select(poi_t.star)
             .where(poi_t.purchase_order_id == P())
             .orderby(line_order(poi_t)))
        po_items = conn.execute(q.get_sql(), (po_id,)).fetchall()
        for poi_row in po_items:
            poi = row_to_dict(poi_row)
            qty = to_decimal(poi["quantity"])
            invoiced = to_decimal(poi["invoiced_qty"])
            remaining = qty - invoiced
            if remaining <= 0:
                continue
            rate = to_decimal(poi["rate"])
            amount = round_currency(remaining * rate)
            _share = _preview_bill_discount(
                conn, poi, remaining, _bill_intra_qty, _bill_intra_disc)
            total_amount += round_currency(amount - _share)
            _line_id = str(uuid.uuid4())
            _bill_line_discounts.append({
                "line": _line_id,
                "discount_amount": _discount_text(_share),
                "net": str(round_currency(amount - _share))})
            pi_items.append((
                _line_id, pi_id, poi["item_id"],
                str(round_currency(remaining)), poi["uom"],
                str(round_currency(rate)), str(amount),
                None, None, None, poi["id"], None,
                _discount_text(_share),
            ))

    elif pr_id_arg:
        # Create from Purchase Receipt
        q = Q.from_(pr_t_lookup).select(pr_t_lookup.star).where(pr_t_lookup.id == P())
        pr = conn.execute(q.get_sql(), (pr_id_arg,)).fetchone()
        if not pr:
            err(f"Purchase receipt {pr_id_arg} not found")
        if pr["status"] != "submitted":
            err(f"Cannot create a bill from receipt {pr_id_arg}: it is '{pr['status']}' (must be 'submitted')")
        supplier_id = pr["supplier_id"]
        company_id = pr["company_id"]
        update_stock = 0  # Stock already moved via GRN

        pri_t_lookup = Table("purchase_receipt_item")
        q = (Q.from_(pri_t_lookup).select(pri_t_lookup.star)
             .where(pri_t_lookup.purchase_receipt_id == P())
             .orderby(line_order(pri_t_lookup)))
        pr_items = conn.execute(q.get_sql(), (pr_id_arg,)).fetchall()
        for pri_row in pr_items:
            pri = row_to_dict(pri_row)
            qty = to_decimal(pri["quantity"])
            rate = to_decimal(pri["rate"])
            amount = round_currency(qty * rate)
            _rcpt_disc = pri.get("discount_amount") or "0"
            total_amount += round_currency(amount - to_decimal(_rcpt_disc))
            _line_id = str(uuid.uuid4())
            _bill_line_discounts.append({
                "line": _line_id,
                "discount_amount": _rcpt_disc,
                "net": str(round_currency(amount - to_decimal(_rcpt_disc)))})
            pi_items.append((
                _line_id, pi_id, pri["item_id"],
                str(round_currency(qty)), pri.get("uom"),
                str(round_currency(rate)), str(amount),
                None, None, None,
                pri.get("purchase_order_item_id"), pri["id"],
                _rcpt_disc,
            ))

    else:
        # Standalone invoice
        if not supplier_id:
            err("--supplier-id is required for standalone invoice")
        if not company_id:
            err("--company-id is required for standalone invoice")
        if not items_arg:
            err("--items is required for standalone invoice")

        for i, item in enumerate(items_arg):
            item_id = item.get("item_id")
            if not item_id:
                err(f"Item {i}: item_id is required")
            qty = to_decimal(item.get("qty", "0"))
            if qty <= 0:
                err(f"Item {i}: qty must be > 0")
            rate = to_decimal(item.get("rate", "0"))
            if rate <= 0:
                err(f"Item {i}: rate must be > 0")
            amount = round_currency(qty * rate)
            _raw_disc = item.get("discount_amount", "0")
            if _raw_disc is None:
                _raw_disc = "0"
            _given_disc = to_decimal(_raw_disc)
            if _given_disc < 0:
                err(f"Item {i}: discount_amount must not be negative")
            _disc = round_currency(_given_disc)
            if _disc > 0 and _disc >= amount:
                err(f"Item {i}: discount {_disc} must be less than the line amount {amount}")
            total_amount += round_currency(amount - _disc)
            _line_id = str(uuid.uuid4())
            _bill_line_discounts.append({
                "line": _line_id,
                "discount_amount": _discount_text(_disc),
                "net": str(round_currency(amount - _disc))})
            pi_items.append((
                _line_id, pi_id, item_id,
                str(round_currency(qty)), item.get("uom"),
                str(round_currency(rate)), str(amount),
                item.get("expense_account_id"), item.get("cost_center_id"),
                item.get("project_id"), None, None,
                _discount_text(_disc),
            ))

    if not pi_items:
        err("No items for invoice")

    if not po_id and not pr_id_arg:
        # Standalone bill: the company must exist before the party is
        # checked, so an unknown company is not reported as a party mismatch.
        co_t = Table("company")
        coq = Q.from_(co_t).select(co_t.id).where(co_t.id == P())
        if not conn.execute(coq.get_sql(), (company_id,)).fetchone():
            err(f"Company {company_id} not found")

    # Validate supplier. Derived paths inherit supplier and company from the
    # parent; a parent that already mixes companies is refused here, before
    # the first write.
    sup_t = Table("supplier")
    q = Q.from_(sup_t).select(sup_t.star).where(sup_t.id == P())
    supplier = conn.execute(q.get_sql(), (supplier_id,)).fetchone()
    if not supplier:
        err(f"Supplier {supplier_id} not found")
    if supplier["company_id"] != company_id:
        err(f"Supplier {supplier_id} belongs to another company")

    currency = _document_currency(conn, supplier, company_id)

    # Calculate tax
    tax_amount, tax_details = _calculate_tax(conn, tax_template_id, total_amount)
    grand_total = round_currency(total_amount + tax_amount)

    dims_given = _dimension_input(args)
    if dims_given is not None:
        dims_obj = dims_given
        dims_text = dimensions_json_text(dims_given)
    elif po_id:
        parent_text = po["dimensions_json"] if po["dimensions_json"] else "{}"
        dims_text = parent_text
        try:
            dims_obj = json.loads(parent_text)
        except (ValueError, TypeError):
            dims_obj = {}
        if not isinstance(dims_obj, dict):
            dims_obj = {}
    elif pr_id_arg:
        parent_text = pr["dimensions_json"] if pr["dimensions_json"] else "{}"
        dims_text = parent_text
        try:
            dims_obj = json.loads(parent_text)
        except (ValueError, TypeError):
            dims_obj = {}
        if not isinstance(dims_obj, dict):
            dims_obj = {}
    else:
        dims_obj = {}
        dims_text = dimensions_json_text({})
    _validate_dims_before_write(conn, dims_obj)

    # Insert parent first
    pi_t = Table("purchase_invoice")
    q = (Q.into(pi_t)
         .columns("id", "supplier_id", "posting_date", "due_date",
                  "total_amount", "tax_amount", "grand_total",
                  "outstanding_amount", "tax_template_id", "status",
                  "purchase_order_id", "purchase_receipt_id",
                  "update_stock", "cwip_asset_id", "company_id",
                  "currency", "exchange_rate", "dimensions_json")
         .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(),
                 ValueWrapper("draft"), P(), P(), P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(),
        (pi_id, supplier_id, posting_date, due_date,
         str(round_currency(total_amount)), str(round_currency(tax_amount)),
         str(grand_total), str(grand_total),
         tax_template_id, po_id, pr_id_arg, update_stock, cwip_asset_id, company_id,
         currency, "1", dims_text))

    # Insert items
    pii_t = Table("purchase_invoice_item")
    pii_q = (Q.into(pii_t)
             .columns("id", "purchase_invoice_id", "item_id", "quantity",
                      "uom", "rate", "amount", "expense_account_id",
                      "cost_center_id", "project_id",
                      "purchase_order_item_id", "purchase_receipt_item_id",
                      "discount_amount")
             .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(),
                     P(), P()))
    pii_sql = pii_q.get_sql()
    for row_params in pi_items:
        conn.execute(pii_sql, row_params)

    audit_values = {"supplier_id": supplier_id,
                    "grand_total": str(grand_total),
                    "update_stock": update_stock,
                    "line_discounts": _bill_line_discounts}
    intake_source = getattr(args, "_intake_source_message_id", None)
    if intake_source is not None:
        audit_values["intake_source"] = getattr(args, "_intake_source_kind", "email")
        audit_values["source_message_id"] = intake_source
        capture_digest = getattr(args, "_intake_capture_sha256", None)
        if capture_digest is not None:
            audit_values["capture_sha256"] = capture_digest
    audit(conn, "erpclaw-buying", "create-purchase-invoice", "purchase_invoice", pi_id,
          new_values=audit_values)
    conn.commit()
    ok({"purchase_invoice_id": pi_id,
         "total_amount": str(round_currency(total_amount)),
         "tax_amount": str(round_currency(tax_amount)),
         "grand_total": str(grand_total),
         "currency": currency,
         "update_stock": update_stock})


# ---------------------------------------------------------------------------
# 26. update-purchase-invoice
# ---------------------------------------------------------------------------

def update_purchase_invoice(conn, args):
    """Update a draft purchase invoice."""
    if not args.purchase_invoice_id:
        err("--purchase-invoice-id is required")

    pi_t = Table("purchase_invoice")
    q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
    pi = conn.execute(q.get_sql(), (args.purchase_invoice_id,)).fetchone()
    if not pi:
        err(f"Purchase invoice {args.purchase_invoice_id} not found")
    if pi["status"] != "draft":
        err(f"Cannot update: invoice is '{pi['status']}' (must be 'draft')",
             suggestion="Cancel the document first, then make changes.")

    dims_given = _dimension_input(args)
    old_dims_text = pi["dimensions_json"] if pi["dimensions_json"] else "{}"
    is_return_row = bool(pi["is_return"]) if "is_return" in pi.keys() else False
    if is_return_row:
        orig_t = Table("purchase_invoice")
        oq = Q.from_(orig_t).select(orig_t.star).where(orig_t.id == P())
        orig_row = conn.execute(oq.get_sql(), (pi["return_against"],)).fetchone()
        orig_text = orig_row["dimensions_json"] if orig_row and orig_row["dimensions_json"] else "{}"
        if dims_given is not None and dimensions_json_text(dims_given) != orig_text:
            err(f"Debit note dimensions must match the original invoice's ({orig_text})")
        new_dims_text = dimensions_json_text(dims_given) if dims_given is not None else None
    else:
        if dims_given is not None:
            _validate_dims_before_write(conn, dims_given)
            new_dims_text = dimensions_json_text(dims_given)
        else:
            new_dims_text = None

    updated_fields = []

    if new_dims_text is not None:
        q = (Q.update(pi_t)
             .set(pi_t.dimensions_json, P())
             .where(pi_t.id == P()))
        conn.execute(q.get_sql(), (new_dims_text, args.purchase_invoice_id))
        updated_fields.append("dimensions_json")

    if args.due_date is not None:
        q = (Q.update(pi_t)
             .set(pi_t.due_date, P())
             .where(pi_t.id == P()))
        conn.execute(q.get_sql(), (args.due_date, args.purchase_invoice_id))
        updated_fields.append("due_date")

    if args.items:
        items = _parse_json_arg(args.items, "items")
        if not items or not isinstance(items, list):
            err("--items must be a non-empty JSON array")

        pii_t = Table("purchase_invoice_item")
        _guard_q = (Q.from_(pii_t)
                    .select(pii_t.discount_amount,
                            pii_t.purchase_order_item_id)
                    .where(pii_t.purchase_invoice_id == P()))
        for _grow in conn.execute(
                _guard_q.get_sql(), (args.purchase_invoice_id,)).fetchall():
            if (to_decimal(_grow["discount_amount"] or "0") != 0
                    or _grow["purchase_order_item_id"]):
                err(f"Cannot replace the lines of bill {args.purchase_invoice_id}: it carries order-derived lines; create the bill again from its order or receipt")
        q = Q.from_(pii_t).delete().where(pii_t.purchase_invoice_id == P())
        conn.execute(q.get_sql(), (args.purchase_invoice_id,))

        total_amount = Decimal("0")
        for i, item in enumerate(items):
            item_id = item.get("item_id")
            if not item_id:
                err(f"Item {i}: item_id is required")
            qty = to_decimal(item.get("qty", "0"))
            if qty <= 0:
                err(f"Item {i}: qty must be > 0")
            rate = to_decimal(item.get("rate", "0"))
            if rate <= 0:
                err(f"Item {i}: rate must be > 0")
            amount = round_currency(qty * rate)
            total_amount += amount

            conn.execute(
                """INSERT INTO purchase_invoice_item
                   (id, purchase_invoice_id, item_id, quantity, uom, rate,
                    amount, expense_account_id, cost_center_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), args.purchase_invoice_id, item_id,
                 str(round_currency(qty)), item.get("uom"),
                 str(round_currency(rate)), str(amount),
                 item.get("expense_account_id"), item.get("cost_center_id")),
            )

        tax_amount, _ = _calculate_tax(conn, pi["tax_template_id"], total_amount)
        grand_total = round_currency(total_amount + tax_amount)

        q = (Q.update(pi_t)
             .set(pi_t.total_amount, P())
             .set(pi_t.tax_amount, P())
             .set(pi_t.grand_total, P())
             .set(pi_t.outstanding_amount, P())
             .set(pi_t.updated_at, now())
             .where(pi_t.id == P()))
        conn.execute(q.get_sql(),
            (str(round_currency(total_amount)), str(round_currency(tax_amount)),
             str(grand_total), str(grand_total), args.purchase_invoice_id))
        updated_fields.append("items")

    if not updated_fields:
        err("No fields to update")

    q = (Q.update(pi_t)
         .set(pi_t.updated_at, now())
         .where(pi_t.id == P()))
    conn.execute(q.get_sql(), (args.purchase_invoice_id,))

    if new_dims_text is not None:
        audit(conn, "erpclaw-buying", "update-purchase-invoice", "purchase_invoice",
               args.purchase_invoice_id,
               old_values={"dimensions_json": old_dims_text},
               new_values={"updated_fields": updated_fields,
                           "dimensions_json": new_dims_text})
    else:
        audit(conn, "erpclaw-buying", "update-purchase-invoice", "purchase_invoice",
               args.purchase_invoice_id,
               new_values={"updated_fields": updated_fields})
    conn.commit()
    ok({"purchase_invoice_id": args.purchase_invoice_id,
         "updated_fields": updated_fields})


# ---------------------------------------------------------------------------
# 27. get-purchase-invoice
# ---------------------------------------------------------------------------

def get_purchase_invoice(conn, args):
    """Get purchase invoice with items and payment info."""
    if not args.purchase_invoice_id:
        err("--purchase-invoice-id is required")

    scope_company_id = None
    if getattr(args, "company_id", None) or getattr(args, "company_name", None):
        scope_company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))

    pi_t = Table("purchase_invoice")
    q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
    pi = conn.execute(q.get_sql(), (args.purchase_invoice_id,)).fetchone()
    if not pi:
        err(f"Purchase invoice {args.purchase_invoice_id} not found")
    if scope_company_id and pi["company_id"] != scope_company_id:
        err(f"Purchase invoice {args.purchase_invoice_id} belongs to another company")

    data = row_to_dict(pi)

    pii = Table("purchase_invoice_item").as_("pii")
    i_t = Table("item").as_("i")
    q = (Q.from_(pii)
         .left_join(i_t).on(i_t.id == pii.item_id)
         .select(pii.star, i_t.item_code, i_t.item_name)
         .where(pii.purchase_invoice_id == P())
         .orderby(line_order(pii)))
    items = conn.execute(q.get_sql(), (args.purchase_invoice_id,)).fetchall()
    data["items"] = [row_to_dict(r) for r in items]

    # Payment ledger entries
    ple_t = Table("payment_ledger_entry")
    q = (Q.from_(ple_t).select(ple_t.star)
         .where(ple_t.against_voucher_type == ValueWrapper("purchase_invoice"))
         .where(ple_t.against_voucher_id == P()))
    ple_rows = conn.execute(q.get_sql(), (args.purchase_invoice_id,)).fetchall()
    data["payments"] = [row_to_dict(r) for r in ple_rows]

    ok(data)


# ---------------------------------------------------------------------------
# 28. list-purchase-invoices
# ---------------------------------------------------------------------------

def list_purchase_invoices(conn, args):
    """List purchase invoices."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))
    pi = Table("purchase_invoice").as_("pi")
    s = Table("supplier").as_("s")
    params = []

    count_q = Q.from_(pi).select(fn.Count("*"))
    data_q = (Q.from_(pi)
              .left_join(s).on(s.id == pi.supplier_id)
              .select(pi.star, s.name.as_("supplier_name")))

    count_q = count_q.where(pi.company_id == P())
    data_q = data_q.where(pi.company_id == P())
    params.append(company_id)
    if args.supplier_id:
        count_q = count_q.where(pi.supplier_id == P())
        data_q = data_q.where(pi.supplier_id == P())
        params.append(args.supplier_id)
    if args.pi_status:
        count_q = count_q.where(pi.status == P())
        data_q = data_q.where(pi.status == P())
        params.append(args.pi_status)
    if args.from_date:
        count_q = count_q.where(pi.posting_date >= P())
        data_q = data_q.where(pi.posting_date >= P())
        params.append(args.from_date)
    if args.to_date:
        count_q = count_q.where(pi.posting_date <= P())
        data_q = data_q.where(pi.posting_date <= P())
        params.append(args.to_date)

    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    data_params = params + [limit, offset]

    data_q = (data_q
              .orderby(pi.posting_date, order=Order.desc)
              .orderby(pi.created_at, order=Order.desc)
              .limit(P()).offset(P()))
    rows = conn.execute(data_q.get_sql(), data_params).fetchall()

    ok({"purchase_invoices": [row_to_dict(r) for r in rows],
         "total_count": total_count, "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 3-Way Match Validation (PO - GRN - Invoice)
# ---------------------------------------------------------------------------

def _validate_three_way_match(conn, purchase_invoice_id, items, company_id):
    """Validate 3-way match: PO qty >= GRN qty >= Invoice qty.

    For each invoice item that has a purchase_order_item_id:
    1. Get ordered_qty from the PO item
    2. Get received_qty = SUM of purchase_receipt_item qty where PO item matches
    3. Get previously_invoiced_qty = SUM of other PI item qty where PO item matches
    4. Validate: current_invoice_qty + previously_invoiced_qty <= received_qty

    Company-level policy (three_way_match_policy):
    - 'disabled': skip all checks
    - 'strict': invoice qty must be <= received qty
    - 'tolerant': uses the company's receipt_tolerance_pct for a margin

    Raises ValueError on validation failure.
    """
    # Look up company policy
    company_t = Table("company")
    co_q = (Q.from_(company_t)
            .select(company_t.three_way_match_policy,
                    company_t.receipt_tolerance_pct)
            .where(company_t.id == P()))
    co_row = conn.execute(co_q.get_sql(), (company_id,)).fetchone()

    policy = co_row["three_way_match_policy"] if co_row else "strict"
    tolerance_pct = to_decimal(co_row["receipt_tolerance_pct"]) if co_row else Decimal("0")

    if policy == "disabled":
        return  # Skip 3-way match entirely

    for item_row in items:
        item = row_to_dict(item_row) if not isinstance(item_row, dict) else item_row
        po_item_id = item.get("purchase_order_item_id")
        if not po_item_id:
            continue  # Standalone invoice item — no PO link, skip

        current_qty = to_decimal(item["quantity"])

        # 1. Get ordered qty from PO item
        poi_t = Table("purchase_order_item")
        poi_q = Q.from_(poi_t).select(poi_t.quantity, poi_t.item_id).where(poi_t.id == P())
        poi = conn.execute(poi_q.get_sql(), (po_item_id,)).fetchone()
        if not poi:
            continue  # PO item not found — skip (shouldn't happen)

        ordered_qty = to_decimal(poi["quantity"])
        item_id = poi["item_id"]

        # 2. Get total received qty from all submitted purchase receipt items
        # linked to this PO item
        received_rows = conn.execute(
            """
            SELECT pri.quantity
            FROM purchase_receipt_item pri
            JOIN purchase_receipt pr ON pr.id = pri.purchase_receipt_id
            WHERE pri.purchase_order_item_id = ?
              AND pr.status = 'submitted'
            """,
            (po_item_id,),
        ).fetchall()
        received_qty = sum((to_decimal(r["quantity"]) for r in received_rows), Decimal("0"))

        # 3. Get total previously invoiced qty from other submitted PIs
        # linked to this PO item (exclude current invoice)
        prev_invoiced_rows = conn.execute(
            """
            SELECT pii.quantity
            FROM purchase_invoice_item pii
            JOIN purchase_invoice pi ON pi.id = pii.purchase_invoice_id
            WHERE pii.purchase_order_item_id = ?
              AND pi.status = 'submitted'
              AND pi.id != ?
            """,
            (po_item_id, purchase_invoice_id),
        ).fetchall()
        prev_invoiced_qty = sum((to_decimal(r["quantity"]) for r in prev_invoiced_rows), Decimal("0"))

        total_invoiced = current_qty + prev_invoiced_qty

        # 4. Validate based on policy
        if policy == "tolerant" and tolerance_pct > 0:
            max_allowed = round_currency(
                received_qty * (Decimal("1") + tolerance_pct / Decimal("100"))
            )
        else:
            # strict: no tolerance
            max_allowed = received_qty

        if total_invoiced > max_allowed:
            # Look up item name for a clear error message
            item_t = Table("item")
            item_name_q = Q.from_(item_t).select(item_t.item_name).where(item_t.id == P())
            item_name_row = conn.execute(item_name_q.get_sql(), (item_id,)).fetchone()
            item_name = item_name_row["item_name"] if item_name_row else item_id

            raise ValueError(
                f"Invoice qty exceeds received qty for item '{item_name}'. "
                f"Ordered: {ordered_qty}, Received: {received_qty}, "
                f"Already invoiced: {prev_invoiced_qty}, "
                f"Current invoice: {current_qty}"
            )


# ---------------------------------------------------------------------------
# 29. submit-purchase-invoice
# ---------------------------------------------------------------------------

class _SubmitRefused(Exception):
    pass


def _submit_purchase_invoice_in_txn(conn, purchase_invoice_id, remarks=None) -> dict:
    """Submit purchase invoice: expense GL + AP + tax GL + PLE.
    If update_stock=1, also creates SLE + inventory GL."""

    pi_t = Table("purchase_invoice")
    q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
    pi = conn.execute(q.get_sql(), (purchase_invoice_id,)).fetchone()
    if not pi:
        raise _SubmitRefused(f"Purchase invoice {purchase_invoice_id} not found")
    if pi["status"] != "draft":
        raise _SubmitRefused(f"Cannot submit: invoice is '{pi['status']}' (must be 'draft')")

    pi_dict = row_to_dict(pi)
    company_id = pi_dict["company_id"]
    posting_date = pi_dict["posting_date"]
    supplier_id = pi_dict["supplier_id"]
    update_stock = pi_dict.get("update_stock", 1)
    is_return = bool(pi_dict.get("is_return", 0))
    voucher_type = "debit_note" if is_return else "purchase_invoice"
    try:
        _dims_obj = json.loads(pi_dict.get("dimensions_json") or "{}")
    except (ValueError, TypeError):
        _dims_obj = {}
    if not isinstance(_dims_obj, dict):
        _dims_obj = {}
    _tag_dims = bool(_dims_obj)

    # Verify supplier
    sup_t = Table("supplier")
    q = Q.from_(sup_t).select(sup_t.star).where(sup_t.id == P())
    supplier = conn.execute(q.get_sql(), (supplier_id,)).fetchone()
    if not supplier:
        raise _SubmitRefused(f"Supplier {supplier_id} not found")

    pii_t = Table("purchase_invoice_item")
    q = Q.from_(pii_t).select(pii_t.star).where(pii_t.purchase_invoice_id == P())
    items = conn.execute(q.get_sql(), (purchase_invoice_id,)).fetchall()
    if not items:
        raise _SubmitRefused("Purchase invoice has no items")

    # --- 3-Way Match Validation (PO - GRN - Invoice) ---
    # Only applies to non-return invoices linked to a PO
    if not is_return and pi_dict.get("purchase_order_id"):
        try:
            _validate_three_way_match(
                conn, purchase_invoice_id, items, company_id,
            )
        except ValueError as e:
            raise _SubmitRefused(str(e))

    fiscal_year = get_fiscal_year(conn, posting_date, company_id=company_id)
    cost_center_id = _dims_obj.get("cost_center") or _get_cost_center(conn, company_id)
    naming = get_next_name(conn, "purchase_invoice", company_id=company_id)

    take_chain_heads(conn, [company_id])

    # Decide under the head: the bill may have been deleted or moved out
    # of draft while waiting on the ledger lock. Refuse with the same
    # messages as the pre-head reads, before any ledger or document write.
    _under_head_q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
    _under_head = conn.execute(
        _under_head_q.get_sql(), (purchase_invoice_id,)).fetchone()
    if not _under_head:
        raise _SubmitRefused(f"Purchase invoice {purchase_invoice_id} not found")
    if _under_head["status"] != "draft":
        raise _SubmitRefused(
            f"Cannot submit: invoice is '{_under_head['status']}' (must be 'draft')")

    # Advance invoiced_qty up front (moved ahead of the discount
    # re-derivation so the head take precedes every prior-sum read below).
    # _update_po_invoice_status still runs at the end of submit.
    if not is_return and pi_dict.get("purchase_order_id"):
        for item_row in items:
            item = row_to_dict(item_row)
            if item.get("purchase_order_item_id"):
                # raw SQL — CAST arithmetic expression not expressible in PyPika
                conn.execute(
                    """UPDATE purchase_order_item
                       SET invoiced_qty = CAST(
                           CAST(invoiced_qty AS NUMERIC) + CAST(? AS NUMERIC) AS TEXT)
                       WHERE id = ?""",
                    (item["quantity"], item["purchase_order_item_id"]),
                )
    if is_return and pi_dict.get("return_against"):
        _touch_q = (Q.update(pi_t)
                    .set(pi_t.updated_at, now())
                    .where(pi_t.id == P()))
        conn.execute(_touch_q.get_sql(), (pi_dict["return_against"],))

    # Re-derive every bill line's discount share (rule of record). A
    # receipt-linked line must still match its receipt line exactly; an
    # order-linked line without a receipt link is re-derived; a return line
    # is re-derived as the negated share of its matched billed line.
    # Changed values are written to the draft lines; lines are re-read below.
    _poi_cache = {}
    _bill_match_cache = {}
    _bill_intra_qty = {}
    _bill_intra_disc = {}
    _ret_intra_qty = {}
    _ret_intra_disc = {}
    _rederived = []
    _rederived_old = {}
    _poi_t = Table("purchase_order_item")
    _pri_t = Table("purchase_receipt_item")
    if is_return:
        _against = pi_dict.get("return_against")
        for item_row in items:
            item = row_to_dict(item_row)
            _item_id = item["item_id"]
            if _item_id not in _bill_match_cache:
                if _against:
                    _mq = (Q.from_(pii_t).select(pii_t.star)
                           .where(pii_t.purchase_invoice_id == P())
                           .where(pii_t.item_id == P())
                           .orderby(line_order(pii_t)))
                    _bill_match_cache[_item_id] = conn.execute(
                        _mq.get_sql(), (_against, _item_id)).fetchall()
                else:
                    _bill_match_cache[_item_id] = []
            _Dbill = Decimal("0")
            _Qbill = Decimal("0")
            for _m in _bill_match_cache[_item_id]:
                if to_decimal(_m["discount_amount"] or "0") != 0:
                    _Dbill = to_decimal(_m["discount_amount"])
                    _Qbill = to_decimal(_m["quantity"])
                    break
            _qpos = -to_decimal(item["quantity"])
            if _against:
                _sub_qty, _sub_disc = _return_prior_sums(
                    conn, _against, _item_id)
            else:
                _sub_qty, _sub_disc = Decimal("0"), Decimal("0")
            _prior_qty = _sub_qty + _ret_intra_qty.get(_item_id, Decimal("0"))
            _prior_disc = _sub_disc + _ret_intra_disc.get(_item_id, Decimal("0"))
            _new_val = -discount_share(
                _Dbill, _Qbill, _prior_qty, _prior_disc, _qpos)
            _new_text = _discount_text(_new_val)
            _old_text = item.get("discount_amount") or "0"
            _ret_intra_qty[_item_id] = (
                _ret_intra_qty.get(_item_id, Decimal("0")) + _qpos)
            _ret_intra_disc[_item_id] = (
                _ret_intra_disc.get(_item_id, Decimal("0")) + (-_new_val))
            if _new_text != _old_text:
                _uq = (Q.update(pii_t)
                       .set(pii_t.discount_amount, P())
                       .where(pii_t.id == P()))
                conn.execute(_uq.get_sql(), (_new_text, item["id"]))
                _rederived.append({"line": item["id"],
                                   "old": _old_text, "new": _new_text})
                _rederived_old[item["id"]] = _old_text
    else:
        for item_row in items:
            item = row_to_dict(item_row)
            if item.get("purchase_receipt_item_id"):
                _rq = (Q.from_(_pri_t).select(_pri_t.discount_amount)
                       .where(_pri_t.id == P()))
                _rrow = conn.execute(
                    _rq.get_sql(),
                    (item["purchase_receipt_item_id"],)).fetchone()
                if _rrow is not None:
                    _bill_d = item.get("discount_amount") or "0"
                    _receipt_d = _rrow["discount_amount"] or "0"
                    if to_decimal(_bill_d) != to_decimal(_receipt_d):
                        raise _SubmitRefused(
                            f"Bill line for item {item['item_id']}: discount {_bill_d} differs from its receipt line discount {_receipt_d}; a bill derived from a receipt carries the receipt's discount")
                continue
            _po_item_id = item.get("purchase_order_item_id")
            if not _po_item_id:
                continue
            if _po_item_id not in _poi_cache:
                _pq = (Q.from_(_poi_t).select(_poi_t.star)
                       .where(_poi_t.id == P()))
                _poi_cache[_po_item_id] = conn.execute(
                    _pq.get_sql(), (_po_item_id,)).fetchone()
            _poi = _poi_cache[_po_item_id]
            if _poi is None:
                continue
            _Q = to_decimal(_poi["quantity"])
            _D = round_currency(
                to_decimal(_poi["amount"]) - to_decimal(_poi["net_amount"]))
            _q = to_decimal(item["quantity"])
            _sub_qty, _sub_disc = _bill_prior_sums(conn, _po_item_id)
            _prior_qty = _sub_qty + _bill_intra_qty.get(
                _po_item_id, Decimal("0"))
            _prior_disc = _sub_disc + _bill_intra_disc.get(
                _po_item_id, Decimal("0"))
            _new_val = discount_share(_D, _Q, _prior_qty, _prior_disc, _q)
            # Legacy overflow: a retroactive order discount (set after
            # earlier receipts posted) that swallows this line refuses
            # here, after the invoiced_qty UPDATE above and before anything
            # posts, so a rollback leaves the order line exactly as it was.
            _bill_line_amount = to_decimal(item.get("amount") or "0")
            if _new_val > 0 and _new_val >= _bill_line_amount:
                raise _SubmitRefused(
                    f"Item {item.get('item_id')}: the remaining order discount {_new_val:.2f} "
                    f"is not less than this line's amount {_bill_line_amount:.2f}; cancel and "
                    f"re-receive the earlier receipts of this order line so the discount "
                    f"is spread, then retry")
            _new_text = _discount_text(_new_val)
            _old_text = item.get("discount_amount") or "0"
            _bill_intra_qty[_po_item_id] = (
                _bill_intra_qty.get(_po_item_id, Decimal("0")) + _q)
            _bill_intra_disc[_po_item_id] = (
                _bill_intra_disc.get(_po_item_id, Decimal("0")) + _new_val)
            if _new_text != _old_text:
                _uq = (Q.update(pii_t)
                       .set(pii_t.discount_amount, P())
                       .where(pii_t.id == P()))
                conn.execute(_uq.get_sql(), (_new_text, item["id"]))
                _rederived.append({"line": item["id"],
                                   "old": _old_text, "new": _new_text})
                _rederived_old[item["id"]] = _old_text
    q = Q.from_(pii_t).select(pii_t.star).where(pii_t.purchase_invoice_id == P())
    items = conn.execute(q.get_sql(), (purchase_invoice_id,)).fetchall()

    # A re-derivation that moved any line reprices a stale draft header at
    # the net before any ledger row is written; the three locals below then
    # read the repriced header, so tax, payable and payment legs post net.
    if _rederived:
        _repriced_total = round_currency(sum((
            to_decimal(row_to_dict(_r).get("amount") or "0")
            - to_decimal(row_to_dict(_r).get("discount_amount") or "0")
            for _r in items), Decimal("0")))
        if is_return:
            _repriced_tax = Decimal("0")
        else:
            _repriced_tax, _ = _calculate_tax(
                conn, pi_dict.get("tax_template_id"), _repriced_total)
        _repriced_grand = round_currency(_repriced_total + _repriced_tax)
        _hu = (Q.update(pi_t)
               .set(pi_t.total_amount, P())
               .set(pi_t.tax_amount, P())
               .set(pi_t.grand_total, P())
               .set(pi_t.outstanding_amount, P())
               .where(pi_t.id == P()))
        conn.execute(_hu.get_sql(),
            (str(round_currency(_repriced_total)), str(round_currency(_repriced_tax)),
             str(_repriced_grand), str(_repriced_grand), purchase_invoice_id))
        pi_dict["total_amount"] = str(round_currency(_repriced_total))
        pi_dict["tax_amount"] = str(round_currency(_repriced_tax))
        pi_dict["grand_total"] = str(_repriced_grand)

    total_amount = to_decimal(pi_dict["total_amount"])
    tax_amount = to_decimal(pi_dict["tax_amount"])
    grand_total = to_decimal(pi_dict["grand_total"])

    # S3 CWIP hook (AVA-43): when the bill carries a --cwip-asset-id, route its
    # expense legs to the asset's capital_work_in_progress account and record the
    # accumulation row in THIS submit transaction (input tax stays recoverable and
    # is not capitalised; the accumulated amount is the pre-tax item total).
    cwip_asset_id = pi_dict.get("cwip_asset_id")
    cwip_asset = None
    cwip_acct = None
    if cwip_asset_id:
        if is_return:
            raise _SubmitRefused("--cwip-asset-id bills cannot be returns/debit notes.")
        try:
            cwip_asset = get_under_construction_asset(conn, cwip_asset_id)
            cwip_acct = resolve_cwip_account(conn, cwip_asset_id, company_id)
        except ValueError as e:
            raise _SubmitRefused(str(e))

    # A discounted FIFO line on a stock-moving non-return bill refuses
    # before anything posts, exactly as receipt submit does; a return is
    # outgoing and never takes an incoming value.
    if update_stock and not is_return:
        for item_row in items:
            item = row_to_dict(item_row)
            _bill_fifo_disc = to_decimal(item.get("discount_amount") or "0")
            if _bill_fifo_disc != 0:
                _bill_fifo_msg = check_fifo_discounted_net(
                    conn, item.get("item_id"),
                    round_currency(to_decimal(item.get("amount") or "0")
                                   - _bill_fifo_disc),
                    item.get("quantity"))
                if _bill_fifo_msg is not None:
                    raise _SubmitRefused(_bill_fifo_msg)

    # --- Build GL entries ---
    gl_entries = []

    company_t = Table("company")
    q = Q.from_(company_t).select(company_t.star).where(company_t.id == P())
    company_row = conn.execute(q.get_sql(), (company_id,)).fetchone()
    default_expense_acct = company_row["default_expense_account_id"] if company_row else None

    # Check if this invoice has a linked purchase receipt.
    # If so, the receipt already posted DR Inventory / CR SRNB.
    # The invoice should then DR SRNB / CR Payable (clearing the accrual)
    # rather than DR Expense / CR Payable (which double-counts COGS).
    has_receipt = bool(pi_dict.get("purchase_receipt_id"))
    srnb_acct = None
    acct_t = Table("account")
    if has_receipt:
        q = (Q.from_(acct_t).select(acct_t.id)
             .where(acct_t.account_type == ValueWrapper("stock_received_not_billed"))
             .where(acct_t.company_id == P())
             .where(acct_t.is_group == 0)
             .limit(1))
        srnb_row = conn.execute(q.get_sql(), (company_id,)).fetchone()
        srnb_acct = srnb_row["id"] if srnb_row else None

    # 1. DR: SRNB (if receipt-linked) or Expense accounts (per item or default)
    for item_row in items:
        item = row_to_dict(item_row)
        amount = abs(to_decimal(item["amount"])
                     - to_decimal(item.get("discount_amount") or "0"))
        if amount <= 0:
            continue
        # Use SRNB when clearing a receipt accrual; expense otherwise
        if has_receipt and srnb_acct:
            debit_acct = srnb_acct
        elif has_receipt and not srnb_acct:
            # Perpetual inventory fallback: use Inventory (stock) account
            # when SRNB account doesn't exist but receipt is linked
            inv_q = (Q.from_(acct_t).select(acct_t.id)
                     .where(acct_t.account_type == ValueWrapper("stock"))
                     .where(acct_t.company_id == P())
                     .where(acct_t.is_group == 0)
                     .limit(1))
            inv_row = conn.execute(inv_q.get_sql(), (company_id,)).fetchone()
            if inv_row:
                debit_acct = inv_row["id"]
            else:
                debit_acct = item.get("expense_account_id") or default_expense_acct
        else:
            debit_acct = item.get("expense_account_id") or default_expense_acct
        if cwip_acct:
            # CWIP cost bill: capitalise the item to construction-in-progress.
            debit_acct = cwip_acct
        if not debit_acct:
            raise _SubmitRefused(f"No expense account for item {item['item_id']} and no company default")
        item_cc = item.get("cost_center_id") or cost_center_id
        # Line wins: a line's own cost center replaces the document tag's
        # cost_center in that leg's dimensions before posting, so step 13
        # sees one cost center on the leg.
        if _tag_dims:
            _leg_dims = dict(_dims_obj)
            if item.get("cost_center_id") and "cost_center" in _leg_dims:
                _leg_dims["cost_center"] = item["cost_center_id"]
        else:
            _leg_dims = None
        if is_return:
            # Debit note: CR expense/SRNB (reverse the original DR)
            gl_entries.append({
                "account_id": debit_acct,
                "debit": "0",
                "credit": str(round_currency(amount)),
                "cost_center_id": item_cc,
                "fiscal_year": fiscal_year,
                **({"dimensions": _leg_dims} if _leg_dims is not None else {}),
            })
        else:
            gl_entries.append({
                "account_id": debit_acct,
                "debit": str(round_currency(amount)),
                "credit": "0",
                "cost_center_id": item_cc,
                "fiscal_year": fiscal_year,
                **({"dimensions": _leg_dims} if _leg_dims is not None else {}),
            })

    # 2. DR: Input Tax (if tax exists) — for returns, use abs() and CR
    abs_tax_amount = abs(tax_amount)
    abs_total_amount = abs(total_amount)
    if abs_tax_amount > 0 and pi_dict.get("tax_template_id"):
        ttl_t = Table("tax_template_line").as_("ttl")
        q = (Q.from_(ttl_t)
             .select(ttl_t.tax_account_id, ttl_t.rate)
             .where(ttl_t.tax_template_id == P())
             .orderby(ttl_t.row_order))
        tax_lines = conn.execute(q.get_sql(),
            (pi_dict["tax_template_id"],)).fetchall()
        remaining_tax = abs_tax_amount
        for tl in tax_lines:
            tl_rate = to_decimal(tl["rate"])
            line_tax = round_currency(abs_total_amount * tl_rate / Decimal("100"))
            if line_tax > remaining_tax:
                line_tax = remaining_tax
            if line_tax > 0:
                if is_return:
                    gl_entries.append({
                        "account_id": tl["tax_account_id"],
                        "debit": "0",
                        "credit": str(round_currency(line_tax)),
                        "fiscal_year": fiscal_year,
                        **({"dimensions": dict(_dims_obj)} if _tag_dims else {}),
                    })
                else:
                    gl_entries.append({
                        "account_id": tl["tax_account_id"],
                        "debit": str(round_currency(line_tax)),
                        "credit": "0",
                        "fiscal_year": fiscal_year,
                        **({"dimensions": dict(_dims_obj)} if _tag_dims else {}),
                    })
                remaining_tax -= line_tax
        # If any rounding remainder, add to last tax account
        if remaining_tax > Decimal("0") and tax_lines:
            side = "credit" if is_return else "debit"
            gl_entries[-1][side] = str(round_currency(
                to_decimal(gl_entries[-1][side]) + remaining_tax))

    # 3. CR: Trade Payables / Accounts Payable
    payable_acct = None
    if company_row:
        payable_acct = company_row["default_payable_account_id"]
    if not payable_acct:
        q = (Q.from_(acct_t).select(acct_t.id)
             .where(acct_t.account_type == ValueWrapper("payable"))
             .where(acct_t.company_id == P())
             .where(acct_t.is_group == 0)
             .limit(1))
        payable_row = conn.execute(q.get_sql(), (company_id,)).fetchone()
        payable_acct = payable_row["id"] if payable_row else None
    if not payable_acct:
        raise _SubmitRefused("No payable account found for company")

    abs_grand_total = abs(grand_total)
    if is_return:
        # Debit note: DR payable (reverse the original CR)
        gl_entries.append({
            "account_id": payable_acct,
            "debit": str(round_currency(abs_grand_total)),
            "credit": "0",
            "party_type": "supplier",
            "party_id": supplier_id,
            "fiscal_year": fiscal_year,
            **({"dimensions": dict(_dims_obj)} if _tag_dims else {}),
        })
    else:
        gl_entries.append({
            "account_id": payable_acct,
            "debit": "0",
            "credit": str(round_currency(grand_total)),
            "party_type": "supplier",
            "party_id": supplier_id,
            "fiscal_year": fiscal_year,
            **({"dimensions": dict(_dims_obj)} if _tag_dims else {}),
        })

    # Insert GL entries
    gl_ids = []
    if gl_entries:
        try:
            gl_ids = insert_gl_entries(
                conn, gl_entries,
                voucher_type=voucher_type,
                voucher_id=purchase_invoice_id,
                posting_date=posting_date,
                company_id=company_id,
                remarks=(remarks if remarks is not None
                         else f"{'Debit Note' if is_return else 'Purchase Invoice'} {naming}"),
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-buying] {e}\n")
            raise _SubmitRefused(f"GL posting failed: {e}")

    # S3 CWIP hook (AVA-43): record the accumulation against the routed CWIP leg
    # in this same submit transaction. gl_ids[0] is the first item's DR CWIP leg.
    cwip_accum_id = None
    if cwip_asset_id and gl_ids:
        try:
            cwip_accum_id = record_cwip_accumulation(
                conn, cwip_asset, abs(total_amount),
                source_voucher_type="purchase_invoice",
                source_voucher_id=purchase_invoice_id,
                gl_entry_id=gl_ids[0], accumulated_at=posting_date,
                notes=f"Purchase invoice {naming}")
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-buying] {e}\n")
            raise _SubmitRefused(f"CWIP accumulation failed: {e}")

    # --- SLE if update_stock=1 ---
    sle_ids = []
    if update_stock:
        sle_entries = []
        for item_row in items:
            item = row_to_dict(item_row)
            # Determine item type
            item_t = Table("item")
            q = Q.from_(item_t).select(item_t.is_stock_item).where(item_t.id == P())
            item_master = conn.execute(q.get_sql(), (item["item_id"],)).fetchone()
            if not item_master or not item_master["is_stock_item"]:
                continue  # Skip non-stock items

            qty = to_decimal(item["quantity"])
            rate = to_decimal(item["rate"])
            # Determine warehouse
            warehouse_id = None
            if item.get("purchase_receipt_item_id"):
                pri_t2 = Table("purchase_receipt_item")
                q = Q.from_(pri_t2).select(pri_t2.warehouse_id).where(pri_t2.id == P())
                pri = conn.execute(q.get_sql(),
                    (item["purchase_receipt_item_id"],)).fetchone()
                warehouse_id = pri["warehouse_id"] if pri else None
            if not warehouse_id and item.get("purchase_order_item_id"):
                poi_t2 = Table("purchase_order_item")
                q = Q.from_(poi_t2).select(poi_t2.warehouse_id).where(poi_t2.id == P())
                poi = conn.execute(q.get_sql(),
                    (item["purchase_order_item_id"],)).fetchone()
                warehouse_id = poi["warehouse_id"] if poi else None
            if not warehouse_id and company_row:
                warehouse_id = company_row["default_warehouse_id"]
            if not warehouse_id:
                continue  # Skip if no warehouse

            _bill_sle_entry = {
                "item_id": item["item_id"],
                "warehouse_id": warehouse_id,
                "actual_qty": str(round_currency(qty)),
                "incoming_rate": str(round_currency(rate)),
                "fiscal_year": fiscal_year,
                # FINDING-010 / ADR-0014: bill-posts-stock is a true external receipt —
                # it must carry the bill line rate, never silently book inventory at $0.
                "require_rate": True,
            }
            # A discounted line on a stock-moving non-return bill values
            # stock at exactly its net; a return is outgoing and takes none.
            _bill_sle_disc = to_decimal(item.get("discount_amount") or "0")
            if not is_return and _bill_sle_disc != 0:
                _bill_sle_entry["incoming_value"] = str(
                    to_decimal(item.get("amount") or "0") - _bill_sle_disc)
            sle_entries.append(_bill_sle_entry)

        if sle_entries:
            try:
                sle_ids = insert_sle_entries(
                    conn, sle_entries,
                    voucher_type=voucher_type,
                    voucher_id=purchase_invoice_id,
                    posting_date=posting_date,
                    company_id=company_id,
                )
            except ValueError as e:
                sys.stderr.write(f"[erpclaw-buying] {e}\n")
                raise _SubmitRefused(f"SLE posting failed: {e}")

            # Inventory GL for SLE (DR Stock In Hand / CR Stock Received Not Billed)
            sle_t = Table("stock_ledger_entry")
            q = (Q.from_(sle_t).select(sle_t.star)
                 .where(sle_t.voucher_type == P())
                 .where(sle_t.voucher_id == P())
                 .where(sle_t.is_cancelled == 0))
            sle_rows = conn.execute(q.get_sql(),
                (voucher_type, purchase_invoice_id)).fetchall()
            sle_dicts = [row_to_dict(r) for r in sle_rows]
            try:
                if _tag_dims:
                    inv_gl = create_perpetual_inventory_gl(
                        conn, sle_dicts,
                        voucher_type=voucher_type,
                        voucher_id=purchase_invoice_id,
                        posting_date=posting_date,
                        company_id=company_id,
                        cost_center_id=cost_center_id,
                        dimensions=dict(_dims_obj),
                    )
                else:
                    inv_gl = create_perpetual_inventory_gl(
                        conn, sle_dicts,
                        voucher_type=voucher_type,
                        voucher_id=purchase_invoice_id,
                        posting_date=posting_date,
                        company_id=company_id,
                        cost_center_id=cost_center_id,
                    )
            except ValueError as e:
                sys.stderr.write(f"[erpclaw-buying] {e}\n")
                raise _SubmitRefused(f"GL posting failed: {e}")
            if inv_gl:
                for gle in inv_gl:
                    gle["fiscal_year"] = fiscal_year
                # Insert stock/COGS GL entries via shared lib (entry_set="cogs"
                # allows multiple GL sets per voucher without idempotency conflict)
                stock_remark = f"{'Debit Note' if is_return else 'Purchase Invoice'} Stock {naming}"
                try:
                    cogs_gl_ids = insert_gl_entries(
                        conn, inv_gl,
                        voucher_type=voucher_type,
                        voucher_id=purchase_invoice_id,
                        posting_date=posting_date,
                        company_id=company_id,
                        remarks=stock_remark,
                        entry_set="cogs",
                    )
                    gl_ids.extend(cogs_gl_ids)
                except ValueError as e:
                    sys.stderr.write(f"[erpclaw-buying] {e}\n")
                    raise _SubmitRefused(f"Stock GL posting failed: {e}")

    # --- Create PLE (Payment Ledger Entry) ---
    ple_id = str(uuid.uuid4())
    # For returns: PLE amount is negative (reduces supplier liability)
    # against_voucher points to the original invoice being returned against
    if is_return:
        ple_against_type = "purchase_invoice"
        ple_against_id = pi_dict.get("return_against") or purchase_invoice_id
        ple_amount = str(round_currency(-abs_grand_total))  # Negative to reduce payable
    else:
        ple_against_type = "purchase_invoice"
        ple_against_id = purchase_invoice_id
        ple_amount = str(round_currency(grand_total))
    ple_remark = (remarks if remarks is not None
                  else f"{'Debit Note' if is_return else 'Purchase Invoice'} {naming}")
    ple_t = Table("payment_ledger_entry")
    q = (Q.into(ple_t)
         .columns("id", "posting_date", "account_id", "party_type", "party_id",
                  "voucher_type", "voucher_id", "against_voucher_type",
                  "against_voucher_id", "amount", "amount_in_account_currency",
                  "currency", "remarks")
         .insert(P(), P(), P(), ValueWrapper("supplier"), P(), P(), P(), P(), P(),
                 P(), P(), P(), P()))
    conn.execute(q.get_sql(),
        (ple_id, posting_date, payable_acct, supplier_id,
         voucher_type, purchase_invoice_id,
         ple_against_type, ple_against_id,
         ple_amount, ple_amount, pi_dict.get("currency") or "USD", ple_remark))

    # invoiced_qty was advanced right after the chain-head take above;
    # only the PO status roll-up remains here.
    if pi_dict.get("purchase_order_id"):
        _update_po_invoice_status(conn, pi_dict["purchase_order_id"])

    # Update invoice status (compare-and-set on the decided draft state:
    # a concurrent submit, cancel or delete wins instead of posting twice).
    q = (Q.update(pi_t)
         .set(pi_t.status, ValueWrapper("submitted"))
         .set(pi_t.naming_series, P())
         .set(pi_t.updated_at, now())
         .where(pi_t.id == P())
         .where(pi_t.status == ValueWrapper("draft")))
    _status_cur = conn.execute(q.get_sql(), (naming, purchase_invoice_id))
    if _status_cur.rowcount == 0:
        _lost_q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
        _lost = conn.execute(
            _lost_q.get_sql(), (purchase_invoice_id,)).fetchone()
        if not _lost:
            raise _SubmitRefused(
                f"Purchase invoice {purchase_invoice_id} not found")
        raise _SubmitRefused(
            f"Cannot submit: invoice is '{_lost['status']}' (must be 'draft')")

    _submit_line_discounts = []
    for item_row in items:
        item = row_to_dict(item_row)
        _d = item.get("discount_amount") or "0"
        _a = item.get("amount") or "0"
        _submit_line_discounts.append({
            "line": item["id"], "discount_amount": _d,
            "net": str(round_currency(to_decimal(_a) - to_decimal(_d)))})
    _submit_new = {"naming_series": naming, "is_return": is_return,
                   "voucher_type": voucher_type,
                   "gl_count": len(gl_ids), "sle_count": len(sle_ids),
                   "update_stock": update_stock,
                   "line_discounts": _submit_line_discounts}
    if _rederived:
        _submit_new["discount_rederived"] = _rederived
    audit_action = "submit-debit-note" if is_return else "submit-purchase-invoice"
    if _rederived:
        audit(conn, "erpclaw-buying", audit_action, "purchase_invoice",
               purchase_invoice_id,
               old_values={"discount_amount": _rederived_old},
               new_values=_submit_new)
    else:
        audit(conn, "erpclaw-buying", audit_action, "purchase_invoice",
               purchase_invoice_id,
               new_values=_submit_new)
    resp = {"purchase_invoice_id": purchase_invoice_id,
            "naming_series": naming, "status": "submitted",
            "is_return": is_return, "voucher_type": voucher_type,
            "gl_entries_created": len(gl_ids),
            "sle_entries_created": len(sle_ids),
            "update_stock": bool(update_stock)}
    if _rederived:
        resp["discount_rederived"] = _rederived
    if cwip_accum_id:
        resp["cwip_asset_id"] = cwip_asset_id
        resp["cwip_accumulation_id"] = cwip_accum_id
    return resp


def submit_purchase_invoice(conn, args):
    """Submit purchase invoice: expense GL + AP + tax GL + PLE.
    If update_stock=1, also creates SLE + inventory GL."""
    if not args.purchase_invoice_id:
        err("--purchase-invoice-id is required")

    try:
        resp = _submit_purchase_invoice_in_txn(conn, args.purchase_invoice_id)
    except _SubmitRefused as e:
        conn.rollback()
        err(str(e))
    conn.commit()
    ok(resp)


def _update_po_invoice_status(conn, purchase_order_id):
    """Update PO per_invoiced and status based on invoiced quantities."""
    poi_t = Table("purchase_order_item")
    q = (Q.from_(poi_t)
         .select(poi_t.quantity, poi_t.invoiced_qty)
         .where(poi_t.purchase_order_id == P()))
    po_items = conn.execute(q.get_sql(), (purchase_order_id,)).fetchall()

    total_ordered = Decimal("0")
    total_invoiced = Decimal("0")
    for poi in po_items:
        total_ordered += to_decimal(poi["quantity"])
        total_invoiced += to_decimal(poi["invoiced_qty"])

    if total_ordered > 0:
        per_invoiced = round_currency(total_invoiced / total_ordered * Decimal("100"))
    else:
        per_invoiced = Decimal("0")

    if per_invoiced >= Decimal("100"):
        new_status = "fully_invoiced"
    elif per_invoiced > Decimal("0"):
        new_status = "partially_invoiced"
    else:
        return  # No change needed

    # Only update if it makes sense (don't downgrade from fully_received etc.)
    po_t = Table("purchase_order")
    q = Q.from_(po_t).select(po_t.status).where(po_t.id == P())
    current = conn.execute(q.get_sql(), (purchase_order_id,)).fetchone()
    if current and current["status"] not in ("cancelled",):
        q = (Q.update(po_t)
             .set(po_t.per_invoiced, P())
             .set(po_t.status, P())
             .set(po_t.updated_at, now())
             .where(po_t.id == P()))
        conn.execute(q.get_sql(),
            (str(per_invoiced), new_status, purchase_order_id))


# ---------------------------------------------------------------------------
# 30. cancel-purchase-invoice
# ---------------------------------------------------------------------------

def _reverse_purchase_stock_or_refuse(conn, voucher_type, voucher_id, posting_date, label):
    """Reverse a voucher's stock ledger entries, refusing on failure.

    Zero active rows is normal (an update_stock bill of non-stock items
    posts no SLE rows), so return [] without calling reverse_sle_entries.
    A failed reversal rolls back every write of the action and refuses.
    """
    _t_sle = Table("stock_ledger_entry")
    active_q = (Q.from_(_t_sle)
                .select(fn.Count("*").as_("cnt"))
                .where(_t_sle.voucher_type == P())
                .where(_t_sle.voucher_id == P())
                .where(_t_sle.is_cancelled == 0))
    active = conn.execute(active_q.get_sql(), (voucher_type, voucher_id)).fetchone()["cnt"]
    if not active:
        return []
    try:
        return reverse_sle_entries(conn, voucher_type, voucher_id, posting_date)
    except ValueError as e:
        conn.rollback()
        sys.stderr.write(f"[erpclaw-buying] {e}\n")
        err(f"{label} failed: {e}")


def cancel_purchase_invoice(conn, args):
    """Cancel a submitted purchase invoice: reverse GL + PLE. If update_stock, reverse SLE."""
    if not args.purchase_invoice_id:
        err("--purchase-invoice-id is required")

    pi_t = Table("purchase_invoice")
    q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
    pi = conn.execute(q.get_sql(), (args.purchase_invoice_id,)).fetchone()
    if not pi:
        err(f"Purchase invoice {args.purchase_invoice_id} not found")
    if pi["status"] not in ("submitted", "overdue", "partially_paid"):
        err(f"Cannot cancel: invoice is '{pi['status']}' "
             f"(must be 'submitted', 'overdue', or 'partially_paid')")

    take_chain_heads(conn, [pi["company_id"]])

    pi = conn.execute(q.get_sql(), (args.purchase_invoice_id,)).fetchone()
    if not pi:
        conn.rollback()
        err(f"Purchase invoice {args.purchase_invoice_id} not found")
    if pi["status"] not in ("submitted", "overdue", "partially_paid"):
        conn.rollback()
        err(f"Cannot cancel: invoice is '{pi['status']}' "
             f"(must be 'submitted', 'overdue', or 'partially_paid')")

    pi_dict = row_to_dict(pi)
    posting_date = pi_dict["posting_date"]
    update_stock = pi_dict.get("update_stock", 0)
    is_return = bool(pi_dict.get("is_return", 0))
    cancel_voucher_type = "debit_note" if is_return else "purchase_invoice"

    # Reverse GL
    try:
        reversal_gl_ids = reverse_gl_entries(
            conn,
            voucher_type=cancel_voucher_type,
            voucher_id=args.purchase_invoice_id,
            posting_date=posting_date,
        )
    except ValueError as e:
        if to_decimal(pi_dict.get("grand_total", "0")) == 0:
            reversal_gl_ids = []
        else:
            err(f"GL reversal failed: {e}")

    # Stock/COGS GL entries (entry_set="cogs") are reversed by the same call above
    # since reverse_gl_entries finds ALL entries for (voucher_type, voucher_id)

    # S3 CWIP hook (AVA-43): if this bill capitalised cost to a CWIP asset, unwind
    # the accumulation row + asset carrying value (its CWIP GL leg was just reversed).
    if pi_dict.get("cwip_asset_id"):
        reverse_cwip_accumulations(conn, "purchase_invoice", args.purchase_invoice_id)

    # Reverse SLE if update_stock
    reversal_sle_ids = []
    if update_stock:
        reversal_sle_ids = _reverse_purchase_stock_or_refuse(conn, cancel_voucher_type, args.purchase_invoice_id, posting_date, "SLE reversal")

    # Release the payment allocations this cancel voids (M46/F1). Cash applied
    # to a bill that no longer exists in the books is not applied cash: the
    # allocation is delinked, its per-allocation PLE rows are closed out, and
    # the payment's residual comes back. Same voucher_type this function uses
    # for its GL and PLE handling, executed by the neutral clearing lib so
    # buying never writes payments' tables itself.
    from erpclaw_lib.payment_clearing import release_allocations_on_document
    release = release_allocations_on_document(
        conn, cancel_voucher_type, args.purchase_invoice_id)

    # Cancel PLE entries
    ple_t = Table("payment_ledger_entry")
    q = (Q.update(ple_t)
         .set(ple_t.delinked, 1)
         .set(ple_t.updated_at, now())
         .where(ple_t.voucher_type == P())
         .where(ple_t.voucher_id == P()))
    conn.execute(q.get_sql(), (cancel_voucher_type, args.purchase_invoice_id))

    # Reverse PO invoiced_qty if linked
    if pi_dict.get("purchase_order_id"):
        pii_t = Table("purchase_invoice_item")
        q = Q.from_(pii_t).select(pii_t.star).where(pii_t.purchase_invoice_id == P())
        items = conn.execute(q.get_sql(), (args.purchase_invoice_id,)).fetchall()
        for item_row in items:
            item = row_to_dict(item_row)
            if item.get("purchase_order_item_id"):
                # raw SQL — CAST+MAX arithmetic expression not expressible in PyPika
                conn.execute(
                    f"""UPDATE purchase_order_item
                       SET invoiced_qty = CAST(
                           {scalar_max("0", "CAST(invoiced_qty AS NUMERIC) - CAST(? AS NUMERIC)")} AS TEXT)
                       WHERE id = ?""",
                    (item["quantity"], item["purchase_order_item_id"]),
                )
        _update_po_invoice_status(conn, pi_dict["purchase_order_id"])

    q = (Q.update(pi_t)
         .set(pi_t.status, ValueWrapper("cancelled"))
         .set(pi_t.updated_at, now())
         .where(pi_t.id == P())
         .where(pi_t.status.isin([ValueWrapper("submitted"), ValueWrapper("overdue"), ValueWrapper("partially_paid")])))
    _cancel_cur = conn.execute(q.get_sql(), (args.purchase_invoice_id,))
    if _cancel_cur.rowcount == 0:
        conn.rollback()
        _fresh = conn.execute(
            Q.from_(pi_t).select(pi_t.status).where(pi_t.id == P()).get_sql(),
            (args.purchase_invoice_id,)).fetchone()
        if _fresh is None:
            err(f"Purchase invoice {args.purchase_invoice_id} not found")
        err(f"Cannot cancel: invoice is '{_fresh['status']}' "
             f"(must be 'submitted', 'overdue', or 'partially_paid')")

    audit_action = "cancel-debit-note" if is_return else "cancel-purchase-invoice"
    audit(conn, "erpclaw-buying", audit_action, "purchase_invoice",
           args.purchase_invoice_id,
           new_values={"reversed": True})
    conn.commit()
    payload = {"purchase_invoice_id": args.purchase_invoice_id,
               "status": "cancelled",
               "gl_reversals": len(reversal_gl_ids),
               "sle_reversals": len(reversal_sle_ids)}
    # Reported only when something was actually released or skipped, so the
    # no-allocation cancel keeps its exact shipped payload (F1 pin 4). A SKIP
    # (payment not submitted, correction C2) is surfaced with the payment id
    # and status, never dropped.
    if release.get("released"):
        payload["allocations_released"] = release["released"]
    if release.get("skipped"):
        payload["allocations_release_skipped"] = release["skipped"]
    ok(payload)


# ---------------------------------------------------------------------------
# 31. create-debit-note
# ---------------------------------------------------------------------------

def create_debit_note(conn, args):
    """Create a debit note (return) against a purchase invoice."""
    if not args.against_invoice_id:
        err("--against-invoice-id is required")
    if not args.items:
        err("--items is required (JSON array)")

    pi_t = Table("purchase_invoice")
    q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
    orig = conn.execute(q.get_sql(), (args.against_invoice_id,)).fetchone()
    if not orig:
        err(f"Purchase invoice {args.against_invoice_id} not found")
    if orig["status"] not in ("submitted", "partially_paid", "paid", "overdue"):
        err(f"Cannot create debit note: invoice status is '{orig['status']}'")

    orig_dict = row_to_dict(orig)
    orig_dims_text = orig["dimensions_json"] if orig["dimensions_json"] else "{}"
    dims_given = _dimension_input(args)
    if dims_given is not None and dimensions_json_text(dims_given) != orig_dims_text:
        err(f"Debit note dimensions must match the original invoice's ({orig_dims_text})")
    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    dn_id = str(uuid.uuid4())
    posting_date = args.posting_date or _today()
    total_amount = Decimal("0")
    _dn_line_discounts = []
    _ret_intra_qty = {}
    _ret_intra_disc = {}

    pii_t = Table("purchase_invoice_item")
    _oq = (Q.from_(pii_t).select(pii_t.star)
           .where(pii_t.purchase_invoice_id == P())
           .orderby(line_order(pii_t)))
    _orig_lines = conn.execute(
        _oq.get_sql(), (args.against_invoice_id,)).fetchall()
    _orig_by_item = {}
    for _ol in _orig_lines:
        _orig_by_item.setdefault(_ol["item_id"], []).append(_ol)
    _orig_has_discount = any(
        to_decimal(_ol["discount_amount"] or "0") != 0
        for _ol in _orig_lines)

    currency = orig_dict.get("currency") or "USD"
    exchange_rate = orig_dict.get("exchange_rate") or "1"
    # Insert parent (is_return=1, negative amounts)
    q = (Q.into(pi_t)
         .columns("id", "supplier_id", "posting_date", "total_amount",
                  "tax_amount", "grand_total", "outstanding_amount", "status",
                  "is_return", "return_against", "update_stock", "company_id",
                  "currency", "exchange_rate", "dimensions_json")
         .insert(P(), P(), P(), ValueWrapper("0"), ValueWrapper("0"),
                 ValueWrapper("0"), ValueWrapper("0"), ValueWrapper("draft"),
                 1, P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(),
        (dn_id, orig_dict["supplier_id"], posting_date,
         args.against_invoice_id, orig_dict.get("update_stock", 0),
         orig_dict["company_id"], currency, exchange_rate, orig_dims_text))

    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        qty = to_decimal(item.get("qty", "0"))
        if qty <= 0:
            err(f"Item {i}: qty must be > 0")
        _matches = _orig_by_item.get(item_id, [])
        if len(_matches) > 1 and _orig_has_discount:
            err(f"Item {i}: item appears on more than one discounted line of the bill; it cannot be returned by item")
        _matched = _matches[0] if len(_matches) == 1 else None
        _matched_disc = (to_decimal(_matched["discount_amount"] or "0")
                         if _matched is not None else Decimal("0"))
        if (_matched is not None and _matched_disc != 0
                and to_decimal(item.get("rate", "0")) > 0):
            err(f"Item {i}: the billed line carries a discount; omit rate so the return uses the billed net")
        rate = to_decimal(item.get("rate", "0"))
        if rate <= 0:
            # Look up rate from original invoice
            q = (Q.from_(pii_t).select(pii_t.rate)
                 .where(pii_t.purchase_invoice_id == P())
                 .where(pii_t.item_id == P())
                 .limit(1))
            orig_item = conn.execute(q.get_sql(),
                (args.against_invoice_id, item_id)).fetchone()
            rate = to_decimal(orig_item["rate"]) if orig_item else Decimal("0")
        if rate <= 0:
            err(f"Item {i}: rate must be > 0")

        # Negate for return
        neg_qty = -qty
        neg_amount = round_currency(neg_qty * rate)

        if _matched is not None and _matched_disc != 0:
            _Dbill = _matched_disc
            _Qbill = to_decimal(_matched["quantity"])
        else:
            _Dbill = Decimal("0")
            _Qbill = Decimal("0")
        _sub_qty, _sub_disc = _return_prior_sums(
            conn, args.against_invoice_id, item_id)
        _prior_qty = _sub_qty + _ret_intra_qty.get(item_id, Decimal("0"))
        _prior_disc = _sub_disc + _ret_intra_disc.get(item_id, Decimal("0"))
        _share = discount_share(_Dbill, _Qbill, _prior_qty, _prior_disc, qty)
        _neg_share = -_share
        total_amount += round_currency(neg_amount - _neg_share)
        _disc_text = _discount_text(_neg_share)
        _ret_intra_qty[item_id] = (
            _ret_intra_qty.get(item_id, Decimal("0")) + qty)
        _ret_intra_disc[item_id] = (
            _ret_intra_disc.get(item_id, Decimal("0")) + _share)
        _dn_line_id = str(uuid.uuid4())
        _dn_line_discounts.append({
            "line": _dn_line_id, "discount_amount": _disc_text,
            "net": str(round_currency(neg_amount - _neg_share))})

        conn.execute(
            """INSERT INTO purchase_invoice_item
               (id, purchase_invoice_id, item_id, quantity, rate, amount,
                discount_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (_dn_line_id, dn_id, item_id,
             str(round_currency(neg_qty)), str(round_currency(rate)),
             str(neg_amount), _disc_text),
        )

    grand_total = total_amount  # Already negative

    q = (Q.update(pi_t)
         .set(pi_t.total_amount, P())
         .set(pi_t.grand_total, P())
         .set(pi_t.outstanding_amount, P())
         .where(pi_t.id == P()))
    conn.execute(q.get_sql(),
        (str(round_currency(total_amount)), str(round_currency(grand_total)),
         str(round_currency(grand_total)), dn_id))

    audit(conn, "erpclaw-buying", "create-debit-note", "purchase_invoice", dn_id,
           new_values={"against_invoice_id": args.against_invoice_id,
                       "reason": args.reason,
                       "total_amount": str(round_currency(total_amount)),
                       "line_discounts": _dn_line_discounts})
    conn.commit()
    ok({"debit_note_id": dn_id,
         "against_invoice_id": args.against_invoice_id,
         "total_amount": str(round_currency(total_amount))})


# ---------------------------------------------------------------------------
# 32. update-invoice-outstanding — RETIRED (M776)
#
# The buying handler (routed as update-purchase-outstanding) reduced a bill's
# outstanding_amount and appended a payment_ledger_entry adjustment row with NO
# general-ledger posting: one call moved a balance the books never saw, while
# the summary-versus-detail checks stayed green because both of their sides
# moved together. It had no production caller — payments clears documents
# in-process through erpclaw_lib.payment_clearing — so the "called by
# erpclaw-payments" line in the old docstring was false.
#
# The action name stays ROUTABLE on purpose (Nik ruling 2026-08-13: retired
# actions STEER to their replacement, the M63-C shape). An agent or an old
# script that asks for a balance move gets one JSON error naming the flows
# that do the job, instead of "Unknown action". The old handler BODY stays
# deleted: its control flow was ok()/err(), which print JSON to stdout and
# sys.exit(), so no in-process caller could ever have used it.
#
# Pinned by buying/tests/test_receipt_invoice.py
# (TestUpdateInvoiceOutstandingRetired: steer + nothing-lands).
# ---------------------------------------------------------------------------

def update_invoice_outstanding(conn, args):
    """RETIRED — see the block above. Steers to the payment, credit-note and write-off flows."""
    from erpclaw_lib.payment_clearing import RETIRED_OUTSTANDING_STEER
    err(
        "'update-purchase-outstanding' has been retired: it moved a document's "
        "balance with no ledger posting.",
        suggestion=RETIRED_OUTSTANDING_STEER,
    )


# ---------------------------------------------------------------------------
# 33. add-landed-cost-voucher
# ---------------------------------------------------------------------------

def add_landed_cost_voucher(conn, args):
    """Allocate landed costs across purchase receipt items."""
    if not args.purchase_receipt_ids:
        err("--purchase-receipt-ids is required (JSON array)")
    if not args.charges:
        err("--charges is required (JSON array)")
    if not args.company_id:
        err("--company-id is required")

    pr_ids = _parse_json_arg(args.purchase_receipt_ids, "purchase-receipt-ids")
    charges = _parse_json_arg(args.charges, "charges")

    if not pr_ids or not isinstance(pr_ids, list):
        err("--purchase-receipt-ids must be a non-empty JSON array")
    if not charges or not isinstance(charges, list):
        err("--charges must be a non-empty JSON array")

    seen_pr_ids = []
    for pr_id in pr_ids:
        if pr_id in seen_pr_ids:
            err(f"Purchase receipt {pr_id} is listed more than once")
        seen_pr_ids.append(pr_id)

    # Validate receipts and gather items
    pr_t = Table("purchase_receipt")
    pr_q = (Q.from_(pr_t).select(pr_t.star)
            .where(pr_t.id == P())
            .where(pr_t.status == ValueWrapper("submitted")))
    pr_sql = pr_q.get_sql()

    pri_t = Table("purchase_receipt_item")
    pri_q = Q.from_(pri_t).select(pri_t.star).where(pri_t.purchase_receipt_id == P())
    pri_sql = pri_q.get_sql()

    all_items = []
    for pr_id in pr_ids:
        pr = conn.execute(pr_sql, (pr_id,)).fetchone()
        if not pr:
            err(f"Purchase receipt {pr_id} not found or not submitted")
        if pr["company_id"] != args.company_id:
            err(f"Purchase receipt {pr_id} belongs to another company")
        items = conn.execute(pri_sql, (pr_id,)).fetchall()
        for item_row in items:
            all_items.append(row_to_dict(item_row))

    if not all_items:
        err("No items found in the specified purchase receipts")

    # Calculate total qty and value for allocation (value on the net basis:
    # amount minus the receipt line's share of the order-line discount)
    def _lcv_net(it):
        return to_decimal(it["amount"]) - to_decimal(
            it.get("discount_amount") or "0")
    total_qty = sum(to_decimal(it["quantity"]) for it in all_items)
    total_value = sum(_lcv_net(it) for it in all_items)

    lcv_id = str(uuid.uuid4())
    posting_date = _today()
    total_landed_cost = Decimal("0")

    # Pre-write check: every charge must credit an account that can hold the
    # carrier's bill (the expense account the bill was recorded against, or an
    # accrual liability the bill will later clear). Refuse before anything is
    # written so a late refusal cannot leave voucher/charge/item rows behind.
    _lcv_acct_t = Table("account")
    _lcv_acct_q = Q.from_(_lcv_acct_t).select(_lcv_acct_t.star).where(_lcv_acct_t.id == P())
    _lcv_acct_sql = _lcv_acct_q.get_sql()
    _lcv_acct_names = []
    for c_idx, charge in enumerate(charges):
        _lcv_amt = to_decimal(charge.get("amount", "0"))
        if _lcv_amt <= 0:
            err(f"Charge {c_idx}: amount must be > 0")
        _lcv_exp_id = charge.get("expense_account_id")
        if not _lcv_exp_id:
            err(f"Charge {c_idx}: expense_account_id is required "
                "(landed cost capitalisation must credit an expense/clearing account)")
        _lcv_found = conn.execute(_lcv_acct_sql, (_lcv_exp_id,)).fetchone()
        if not _lcv_found:
            err(f"Charge {c_idx}: account {_lcv_exp_id} not found")
        _lcv_acct = row_to_dict(_lcv_found)
        _lcv_name = _lcv_acct["name"]
        if _lcv_acct["company_id"] != args.company_id:
            err(f"Charge {c_idx}: account '{_lcv_name}' belongs to a different company")
        if _lcv_acct["is_group"]:
            err(f"Charge {c_idx}: account '{_lcv_name}' is a group account")
        if _lcv_acct["disabled"]:
            err(f"Charge {c_idx}: account '{_lcv_name}' is disabled")
        _lcv_root = _lcv_acct["root_type"]
        _lcv_type = _lcv_acct.get("account_type")
        if _lcv_type == "":
            _lcv_type = None
        _lcv_accepted = ((_lcv_root == "expense" and (_lcv_type is None or _lcv_type == "expense"))
                         or (_lcv_root == "liability" and _lcv_type is None))
        if not _lcv_accepted:
            _lcv_kind = _lcv_type.replace("_", " ") if _lcv_type else _lcv_root
            err(f"Charge {c_idx}: account '{_lcv_name}' ({_lcv_kind}) cannot hold a carrier's bill; "
                "credit the expense account the carrier's bill was recorded against, "
                "or an accrual liability the bill will clear")
        _lcv_acct_names.append(_lcv_name)

    # Resolve the company's stock account once, still before any write.
    _lcv_stock_t = Table("account")
    _lcv_stock_q = (Q.from_(_lcv_stock_t).select(_lcv_stock_t.id)
         .where(_lcv_stock_t.account_type == ValueWrapper("stock"))
         .where(_lcv_stock_t.company_id == P())
         .where(_lcv_stock_t.is_group == 0)
         .limit(1))
    _lcv_stock_row = conn.execute(_lcv_stock_q.get_sql(), (args.company_id,)).fetchone()
    stock_acct_id = _lcv_stock_row["id"] if _lcv_stock_row else None
    if not stock_acct_id:
        err(f"No Stock-in-Hand account (account_type='stock') found for "
            f"company {args.company_id}; cannot capitalise landed cost")

    _lcv_notes = []
    for c_idx, charge in enumerate(charges):
        _lcv_desc = charge.get("description", f"Charge {c_idx + 1}")
        _lcv_notes.append(
            f"The carrier's bill for '{_lcv_desc}' must already be recorded against "
            f"'{_lcv_acct_names[c_idx]}'; this voucher moves that cost into stock "
            "and records nothing owed.")

    # Insert parent first
    lcv_t = Table("landed_cost_voucher")
    q = (Q.into(lcv_t)
         .columns("id", "posting_date", "total_landed_cost", "status", "company_id")
         .insert(P(), P(), ValueWrapper("0"), ValueWrapper("submitted"), P()))
    conn.execute(q.get_sql(), (lcv_id, posting_date, args.company_id))

    fiscal_year = get_fiscal_year(conn, posting_date, company_id=args.company_id)
    cost_center_id = _get_cost_center(conn, args.company_id)
    gl_entries = []

    # Process each charge
    for c_idx, charge in enumerate(charges):
        desc = charge.get("description", f"Charge {c_idx + 1}")
        charge_amount = to_decimal(charge.get("amount", "0"))
        if charge_amount <= 0:
            err(f"Charge {c_idx}: amount must be > 0")
        alloc_method = charge.get("allocation_method", "value")
        expense_account_id = charge.get("expense_account_id")
        # D1 / ADR-0030: every charge is capitalised into stock via the GL AND
        # mirrored in the SLE valuation half. A charge without a credit-side
        # account would silently skip the GL while the SLE delta still posts,
        # guaranteeing an INV-24 (stock GL ≡ SLE valuation) violation — so the
        # account is mandatory, not optional.
        if not expense_account_id:
            err(f"Charge {c_idx}: expense_account_id is required "
                "(landed cost capitalisation must credit an expense/clearing account)")

        total_landed_cost += charge_amount

        # Insert charge record
        lcc_t = Table("landed_cost_charge")
        q = (Q.into(lcc_t)
             .columns("id", "landed_cost_voucher_id", "description", "amount",
                      "expense_account_id", "allocation_method")
             .insert(P(), P(), P(), P(), P(), P()))
        conn.execute(q.get_sql(),
            (str(uuid.uuid4()), lcv_id, desc, str(round_currency(charge_amount)),
             expense_account_id,
             "by_qty" if alloc_method == "qty" else "by_amount"))

        # Allocate charge across receipt items
        allocated_so_far = Decimal("0")
        for idx, item in enumerate(all_items):
            if alloc_method == "qty":
                item_qty = to_decimal(item["quantity"])
                if total_qty > 0:
                    proportion = item_qty / total_qty
                else:
                    proportion = Decimal("1") / Decimal(str(len(all_items)))
            else:  # value
                item_value = _lcv_net(item)
                if total_value > 0:
                    proportion = item_value / total_value
                else:
                    proportion = Decimal("1") / Decimal(str(len(all_items)))

            if idx == len(all_items) - 1:
                allocated_amount = round_currency(charge_amount - allocated_so_far)
            else:
                allocated_amount = round_currency(charge_amount * proportion)
            allocated_so_far += allocated_amount

            _lcv_disc = to_decimal(item.get("discount_amount") or "0")
            if _lcv_disc == 0:
                original_rate = to_decimal(item["rate"])
            else:
                item_qty = to_decimal(item["quantity"])
                _lcv_net_amount = to_decimal(item["amount"]) - _lcv_disc
                if item_qty != 0:
                    original_rate = round_currency(
                        _lcv_net_amount / item_qty)
                else:
                    original_rate = to_decimal(item["rate"])
            item_qty = to_decimal(item["quantity"])
            per_unit_charge = round_currency(allocated_amount / item_qty) if item_qty > 0 else Decimal("0")
            final_rate = round_currency(original_rate + per_unit_charge)

            lci_t = Table("landed_cost_item")
            q = (Q.into(lci_t)
                 .columns("id", "landed_cost_voucher_id", "purchase_receipt_id",
                          "purchase_receipt_item_id", "applicable_charges",
                          "original_rate", "final_rate")
                 .insert(P(), P(), P(), P(), P(), P(), P()))
            conn.execute(q.get_sql(),
                (str(uuid.uuid4()), lcv_id, item["purchase_receipt_id"],
                 item["id"], str(round_currency(allocated_amount)),
                 str(round_currency(original_rate)),
                 str(final_rate)))

        # GL: DR Stock In Hand / CR Expense account for this charge
        gl_entries.append({
            "account_id": stock_acct_id,
            "debit": str(round_currency(charge_amount)),
            "credit": "0",
            "fiscal_year": fiscal_year,
        })
        gl_entries.append({
            "account_id": expense_account_id,
            "debit": "0",
            "credit": str(round_currency(charge_amount)),
            "cost_center_id": cost_center_id,
            "fiscal_year": fiscal_year,
        })

    # Update total
    q = (Q.update(lcv_t)
         .set(lcv_t.total_landed_cost, P())
         .where(lcv_t.id == P()))
    conn.execute(q.get_sql(), (str(round_currency(total_landed_cost)), lcv_id))

    # Insert GL entries
    gl_ids = []
    if gl_entries:
        try:
            gl_ids = insert_gl_entries(
                conn, gl_entries,
                voucher_type="landed_cost_voucher",
                voucher_id=lcv_id,
                posting_date=posting_date,
                company_id=args.company_id,
                remarks=f"Landed Cost Voucher",
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-buying] {e}\n")
            conn.rollback()
            err(f"GL posting failed: {e}")

    # --- Valuation half (D1 / ADR-0030): reprice the stock subledger so the GL
    # Stock-in-Hand debit is mirrored in the SLE (INV-24). Aggregate the per-item
    # applicable charges by (item, warehouse) and post one zero-qty valuation SLE
    # per group; for FIFO items the helper also bumps the receipt-sourced layer
    # rates so future issues consume at the landed-cost-inclusive cost. Every
    # allocated dollar is capitalised (the GL debits the full charge to stock), so
    # the SLE deltas sum to total_landed_cost by construction.
    reprice_count = _reprice_landed_cost(conn, lcv_id, args.company_id,
                                         posting_date, fiscal_year)

    audit(conn, "erpclaw-buying", "add-landed-cost-voucher", "landed_cost_voucher", lcv_id,
           new_values={"total_landed_cost": str(round_currency(total_landed_cost)),
                       "receipt_count": len(pr_ids),
                       "sle_repricings": reprice_count})
    conn.commit()
    ok({"landed_cost_voucher_id": lcv_id,
         "total_landed_cost": str(round_currency(total_landed_cost)),
         "gl_entries_created": len(gl_ids),
         "sle_repricings": reprice_count,
         "notes": _lcv_notes})


def _reprice_landed_cost(conn, lcv_id, company_id, posting_date, fiscal_year, negate=False):
    """Post the SLE valuation half of a landed-cost voucher (or its reversal).

    Groups the voucher's landed_cost_items by (item, resolved warehouse), sums the
    applicable charges, and calls reprice_stock_valuation once per group. On the
    forward post the value delta is the positive charge; on cancel (negate=True)
    it is negated so both the SLE stock_value and any FIFO layer rate bumps are
    restored. Returns the number of (item, warehouse) groups repriced.

    Warehouse resolution mirrors submit-purchase-receipt: a receipt line with no
    warehouse posted its stock to the company default, so that is where it must be
    repriced. A group with no resolvable warehouse is skipped (INV-24 would then
    correctly flag the residual GL-vs-SLE gap rather than it being hidden).
    """
    co_row = conn.execute(
        "SELECT default_warehouse_id FROM company WHERE id = ?", (company_id,)
    ).fetchone()
    default_wh = co_row["default_warehouse_id"] if co_row else None

    rows = conn.execute(
        """
        SELECT pri.item_id AS item_id, pri.warehouse_id AS warehouse_id,
               lci.applicable_charges AS applicable_charges,
               lci.purchase_receipt_id AS pr_id
        FROM landed_cost_item lci
        JOIN purchase_receipt_item pri ON lci.purchase_receipt_item_id = pri.id
        WHERE lci.landed_cost_voucher_id = ?
        """,
        (lcv_id,),
    ).fetchall()

    groups = {}  # (item_id, warehouse_id) -> {"delta": Decimal, "pr_ids": set}
    for r in rows:
        wh_id = r["warehouse_id"] or default_wh
        if not wh_id:
            continue
        key = (r["item_id"], wh_id)
        g = groups.setdefault(key, {"delta": Decimal("0"), "pr_ids": set()})
        g["delta"] += to_decimal(r["applicable_charges"])
        if r["pr_id"]:
            g["pr_ids"].add(r["pr_id"])

    repriced = 0
    for (item_id, wh_id), g in groups.items():
        delta = round_currency(g["delta"])
        if negate:
            delta = round_currency(-delta)
        if delta == 0:
            continue
        reprice_stock_valuation(
            conn, item_id, wh_id,
            voucher_type="landed_cost_voucher",
            voucher_id=lcv_id,
            posting_date=posting_date,
            value_delta=str(delta),
            fiscal_year=fiscal_year,
            fifo_source_voucher_ids=sorted(g["pr_ids"]),
            fifo_source_voucher_type="purchase_receipt",
        )
        repriced += 1
    return repriced


# ---------------------------------------------------------------------------
# 33b. list / get / cancel landed-cost-voucher (lifecycle)
# ---------------------------------------------------------------------------

def list_landed_cost_vouchers(conn, args):
    """List landed cost vouchers for a company (newest first)."""
    if not args.company_id:
        err("--company-id is required")

    limit = int(args.limit or "20")
    offset = int(args.offset or "0")

    lcv_t = Table("landed_cost_voucher")
    q = (Q.from_(lcv_t)
         .select(lcv_t.id, lcv_t.naming_series, lcv_t.posting_date,
                 lcv_t.total_landed_cost, lcv_t.status)
         .where(lcv_t.company_id == P()))
    params = [args.company_id]
    if getattr(args, "lcv_status", None):
        q = q.where(lcv_t.status == P())
        params.append(args.lcv_status)
    q = q.orderby(lcv_t.posting_date, order=Order.desc).limit(limit).offset(offset)
    rows = conn.execute(q.get_sql(), tuple(params)).fetchall()

    count_q = (Q.from_(lcv_t).select(fn.Count("*").as_("cnt"))
               .where(lcv_t.company_id == P()))
    cparams = [args.company_id]
    if getattr(args, "lcv_status", None):
        count_q = count_q.where(lcv_t.status == P())
        cparams.append(args.lcv_status)
    total = conn.execute(count_q.get_sql(), tuple(cparams)).fetchone()["cnt"]

    ok({"landed_cost_vouchers": [row_to_dict(r) for r in rows],
        "total_count": total})


def get_landed_cost_voucher(conn, args):
    """Return a landed cost voucher with its charges and repriced items."""
    if not args.landed_cost_voucher_id:
        err("--landed-cost-voucher-id is required")

    lcv_t = Table("landed_cost_voucher")
    q = Q.from_(lcv_t).select(lcv_t.star).where(lcv_t.id == P())
    lcv = conn.execute(q.get_sql(), (args.landed_cost_voucher_id,)).fetchone()
    if not lcv:
        err(f"Landed cost voucher {args.landed_cost_voucher_id} not found")

    lcc_t = Table("landed_cost_charge")
    cq = (Q.from_(lcc_t).select(lcc_t.star)
          .where(lcc_t.landed_cost_voucher_id == P()))
    charges = conn.execute(cq.get_sql(), (args.landed_cost_voucher_id,)).fetchall()

    lci_t = Table("landed_cost_item")
    iq = (Q.from_(lci_t).select(lci_t.star)
          .where(lci_t.landed_cost_voucher_id == P()))
    items = conn.execute(iq.get_sql(), (args.landed_cost_voucher_id,)).fetchall()

    result = row_to_dict(lcv)
    result["charges"] = [row_to_dict(r) for r in charges]
    result["items"] = [row_to_dict(r) for r in items]
    ok(result)

def list_landed_cost_voucher_anomalies(conn, args):
    """List submitted vouchers naming another company's receipt line or repeating a line."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))

    limit = int(args.limit or "20")
    offset = int(args.offset or "0")

    lcv_t = Table("landed_cost_voucher")
    lci_t = Table("landed_cost_item")
    lcc_t = Table("landed_cost_charge")
    pri_t = Table("purchase_receipt_item")
    pr_t = Table("purchase_receipt")

    vq = (Q.from_(lcv_t)
          .select(lcv_t.id, lcv_t.naming_series, lcv_t.posting_date)
          .where(lcv_t.company_id == P())
          .where(lcv_t.status == ValueWrapper("submitted")))
    info = {}
    for row in conn.execute(vq.get_sql(), (company_id,)).fetchall():
        detail = row_to_dict(row)
        info[detail["id"]] = detail

    anomalies = []
    if info:
        vids = sorted(info)
        oq = (Q.from_(lci_t)
              .join(lcv_t).on(lci_t.landed_cost_voucher_id == lcv_t.id)
              .join(pri_t).on(pri_t.id == lci_t.purchase_receipt_item_id)
              .join(pr_t).on(pr_t.id == pri_t.purchase_receipt_id)
              .select(lci_t.landed_cost_voucher_id,
                      pr_t.id.as_("pr_id"),
                      pri_t.id.as_("line_id"))
              .where(lcv_t.company_id == P())
              .where(lcv_t.status == ValueWrapper("submitted"))
              .where(pr_t.company_id != lcv_t.company_id))
        other = {}
        for row in conn.execute(oq.get_sql(), (company_id,)).fetchall():
            found = row_to_dict(row)
            key = (found["landed_cost_voucher_id"], found["pr_id"])
            other.setdefault(key, set()).add(found["line_id"])
        for (vid, prid), lines in other.items():
            if vid not in info:
                continue
            anomalies.append({
                "landed_cost_voucher_id": vid,
                "naming_series": info[vid]["naming_series"],
                "posting_date": info[vid]["posting_date"],
                "kind": "other-company-receipt",
                "purchase_receipt_id": prid,
                "purchase_receipt_item_ids": sorted(lines)})

        cq = (Q.from_(lcc_t)
              .select(lcc_t.landed_cost_voucher_id, fn.Count("*").as_("cnt"))
              .where(lcc_t.landed_cost_voucher_id.isin([P() for _ in vids]))
              .groupby(lcc_t.landed_cost_voucher_id))
        charges = {}
        for row in conn.execute(cq.get_sql(), tuple(vids)).fetchall():
            counted = row_to_dict(row)
            charges[counted["landed_cost_voucher_id"]] = int(counted["cnt"])

        iq = (Q.from_(lci_t)
              .join(pri_t).on(pri_t.id == lci_t.purchase_receipt_item_id)
              .select(lci_t.landed_cost_voucher_id,
                      lci_t.purchase_receipt_item_id,
                      pri_t.purchase_receipt_id.as_("pr_id"),
                      fn.Count("*").as_("cnt"))
              .where(lci_t.purchase_receipt_item_id.notnull())
              .where(lci_t.landed_cost_voucher_id.isin([P() for _ in vids]))
              .groupby(lci_t.landed_cost_voucher_id,
                       lci_t.purchase_receipt_item_id,
                       pri_t.purchase_receipt_id))
        repeated = {}
        for row in conn.execute(iq.get_sql(), tuple(vids)).fetchall():
            tallied = row_to_dict(row)
            vid = tallied["landed_cost_voucher_id"]
            if vid not in info:
                continue
            allowed = charges.get(vid, 0)
            if allowed < 1:
                allowed = 1
            if int(tallied["cnt"]) > allowed:
                key = (vid, tallied["pr_id"])
                repeated.setdefault(key, set()).add(tallied["purchase_receipt_item_id"])
        for (vid, prid), lines in repeated.items():
            anomalies.append({
                "landed_cost_voucher_id": vid,
                "naming_series": info[vid]["naming_series"],
                "posting_date": info[vid]["posting_date"],
                "kind": "repeated-receipt-item",
                "purchase_receipt_id": prid,
                "purchase_receipt_item_ids": sorted(lines)})

    anomalies.sort(key=lambda entry: (entry["landed_cost_voucher_id"],
                                      entry["kind"],
                                      entry["purchase_receipt_id"]))
    anomalies.sort(key=lambda entry: entry["posting_date"], reverse=True)
    total_count = len(anomalies)
    ok({"company_id": company_id,
        "anomalies": anomalies[offset:offset + limit],
        "total_count": total_count})


def cancel_landed_cost_voucher(conn, args):
    """Cancel a submitted landed cost voucher: reverse the GL AND the SLE valuation.

    Cancel = reverse, never edit. Reverses the constitutional GL posting
    (reverse_gl_entries → flagged mirror rows, originals flagged is_cancelled=1)
    and reverses the valuation half through the shared repricing helper with a
    negated delta (which also restores any FIFO layer rate bumps). INV-24 stays
    green because both the GL stock movement and the SLE value delta net to zero.
    Refuses if the voucher is already cancelled.
    """
    if not args.landed_cost_voucher_id:
        err("--landed-cost-voucher-id is required")

    lcv_t = Table("landed_cost_voucher")
    q = Q.from_(lcv_t).select(lcv_t.star).where(lcv_t.id == P())
    lcv = conn.execute(q.get_sql(), (args.landed_cost_voucher_id,)).fetchone()
    if not lcv:
        err(f"Landed cost voucher {args.landed_cost_voucher_id} not found")
    if lcv["status"] == "cancelled":
        err("Cannot cancel: landed cost voucher is already cancelled")

    lcv_dict = row_to_dict(lcv)
    company_id = lcv_dict["company_id"]
    posting_date = lcv_dict["posting_date"]
    fiscal_year = get_fiscal_year(conn, posting_date, company_id=company_id)

    # Reverse the SLE valuation half (negated delta + FIFO layer restore).
    reprice_count = _reprice_landed_cost(conn, args.landed_cost_voucher_id,
                                         company_id, posting_date, fiscal_year,
                                         negate=True)

    # Reverse the GL (constitutional helper: active mirror rows, originals flagged).
    try:
        reversal_gl_ids = reverse_gl_entries(
            conn,
            voucher_type="landed_cost_voucher",
            voucher_id=args.landed_cost_voucher_id,
            posting_date=posting_date,
        )
    except ValueError:
        reversal_gl_ids = []

    q = (Q.update(lcv_t)
         .set(lcv_t.status, ValueWrapper("cancelled"))
         .set(lcv_t.updated_at, now())
         .where(lcv_t.id == P()))
    conn.execute(q.get_sql(), (args.landed_cost_voucher_id,))

    audit(conn, "erpclaw-buying", "cancel-landed-cost-voucher", "landed_cost_voucher",
          args.landed_cost_voucher_id,
          new_values={"reversed": True, "sle_repricings": reprice_count,
                      "gl_reversals": len(reversal_gl_ids)})
    conn.commit()
    ok({"landed_cost_voucher_id": args.landed_cost_voucher_id,
        "status": "cancelled",
        "sle_repricings": reprice_count,
        "gl_reversals": len(reversal_gl_ids)})


# ---------------------------------------------------------------------------
# 34. status
# ---------------------------------------------------------------------------

def update_receipt_tolerance(conn, args):
    """Update the GRN receipt tolerance percentage for a company.

    Args:
        --company-id: Company to update.
        --tolerance-pct: New tolerance percentage (0 = strict, 5 = allow 5% over).
    """
    if not args.company_id:
        err("--company-id is required")

    pct = to_decimal(args.tolerance_pct or "0")
    if pct < 0:
        err("Tolerance percentage cannot be negative")
    if pct > Decimal("100"):
        err("Tolerance percentage cannot exceed 100%")

    company_t = Table("company")
    q = Q.from_(company_t).select(company_t.id).where(company_t.id == P())
    co = conn.execute(q.get_sql(), (args.company_id,)).fetchone()
    if not co:
        err(f"Company {args.company_id} not found")

    q = (Q.update(company_t)
         .set(company_t.receipt_tolerance_pct, P())
         .set(company_t.updated_at, now())
         .where(company_t.id == P()))
    conn.execute(q.get_sql(), (str(round_currency(pct)), args.company_id))

    audit(conn, "erpclaw-buying", "update-receipt-tolerance", "company",
           args.company_id, new_values={"receipt_tolerance_pct": str(pct)})
    conn.commit()
    ok({"company_id": args.company_id,
         "receipt_tolerance_pct": str(round_currency(pct))})


def update_three_way_match_policy(conn, args):
    """Update the 3-way match policy for a company.

    Args:
        --company-id: Company to update.
        --policy: One of 'strict', 'tolerant', 'disabled'.
    """
    if not args.company_id:
        err("--company-id is required")

    policy = args.policy
    if policy not in ("strict", "tolerant", "disabled"):
        err(f"Invalid policy '{policy}'. Must be 'strict', 'tolerant', or 'disabled'.")

    company_t = Table("company")
    q = Q.from_(company_t).select(company_t.id).where(company_t.id == P())
    co = conn.execute(q.get_sql(), (args.company_id,)).fetchone()
    if not co:
        err(f"Company {args.company_id} not found")

    q = (Q.update(company_t)
         .set(company_t.three_way_match_policy, P())
         .set(company_t.updated_at, now())
         .where(company_t.id == P()))
    conn.execute(q.get_sql(), (policy, args.company_id))

    audit(conn, "erpclaw-buying", "update-three-way-match-policy", "company",
           args.company_id, new_values={"three_way_match_policy": policy})
    conn.commit()
    ok({"company_id": args.company_id,
         "three_way_match_policy": policy})


def status_action(conn, args):
    """Buying summary for a company."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))

    sup_t = Table("supplier")
    q = (Q.from_(sup_t)
         .select(fn.Count("*").as_("cnt"))
         .where(sup_t.company_id == P()))
    supplier_count = conn.execute(q.get_sql(), (company_id,)).fetchone()["cnt"]

    # PO by status
    po_t = Table("purchase_order")
    q = (Q.from_(po_t)
         .select(po_t.status, fn.Count("*").as_("cnt"))
         .where(po_t.company_id == P())
         .groupby(po_t.status))
    po_rows = conn.execute(q.get_sql(), (company_id,)).fetchall()
    po_counts = {}
    for row in po_rows:
        po_counts[row["status"]] = row["cnt"]
    po_counts["total"] = sum(po_counts.values())

    # PI by status
    pi_t = Table("purchase_invoice")
    q = (Q.from_(pi_t)
         .select(pi_t.status, fn.Count("*").as_("cnt"))
         .where(pi_t.company_id == P())
         .groupby(pi_t.status))
    pi_rows = conn.execute(q.get_sql(), (company_id,)).fetchall()
    pi_counts = {}
    for row in pi_rows:
        pi_counts[row["status"]] = row["cnt"]
    pi_counts["total"] = sum(v for k, v in pi_counts.items() if k != "total")

    # Total outstanding
    q = (Q.from_(pi_t)
         .select(fn.Coalesce(DecimalSum(pi_t.outstanding_amount), ValueWrapper("0")).as_("total"))
         .where(pi_t.company_id == P())
         .where(pi_t.status.isin([P(), P(), P()])))
    outstanding = conn.execute(q.get_sql(),
        (company_id, "submitted", "overdue", "partially_paid")).fetchone()
    total_outstanding = round_currency(to_decimal(str(outstanding["total"])))

    ok({
        "suppliers": supplier_count,
        "purchase_orders": po_counts,
        "purchase_invoices": pi_counts,
        "total_outstanding": str(total_outstanding),
    })


# ---------------------------------------------------------------------------
# import-suppliers
# ---------------------------------------------------------------------------

def import_suppliers(conn, args):
    """Bulk import suppliers from a CSV file.

    CSV columns: name, supplier_type (optional), country (optional),
    default_currency (optional), email (optional), phone (optional).
    """
    csv_path = args.csv_path
    company_id = args.company_id
    if not csv_path:
        err("--csv-path is required")
    if not company_id:
        err("--company-id is required")

    # Path safety: resolve symlinks, require .csv extension, must be a regular file
    csv_real = os.path.realpath(csv_path)
    if not csv_real.lower().endswith(".csv"):
        err("--csv-path must point to a .csv file")
    if not os.path.isfile(csv_real):
        err(f"File not found: {csv_path}")

    company_t = Table("company")
    company_q = Q.from_(company_t).select(company_t.default_currency).where(company_t.id == P())
    company_row = conn.execute(company_q.get_sql(), (company_id,)).fetchone()
    if not company_row:
        err(f"Company {company_id} not found")
    company_currency = company_row["default_currency"]

    from erpclaw_lib.csv_import import validate_csv, parse_csv_rows
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args

    errors = validate_csv(csv_real, "supplier")
    if errors:
        err(f"CSV validation failed: {'; '.join(errors)}")

    rows = parse_csv_rows(csv_real, "supplier")
    if not rows:
        err("CSV file is empty")

    import csv
    with open(csv_real, "r", newline="", encoding="utf-8-sig") as handle:
        raw_rows = list(csv.DictReader(handle))
    raw_currencies = [(row.get("default_currency") or "") for row in raw_rows]
    raw_types = [row.get("supplier_type") for row in raw_rows]
    normalised_types = []
    for pos, raw in enumerate(raw_types, start=1):
        if raw is None or not raw.strip():
            normalised_types.append("company")
        else:
            norm = raw.strip().lower()
            if norm not in ("company", "individual"):
                err(f"Row {pos}: supplier_type '{raw}' must be company or individual")
            normalised_types.append(norm)

    imported = 0
    skipped = 0
    for index, row in enumerate(rows):
        name = row.get("name", "")

        sup_t = Table("supplier")
        q = (Q.from_(sup_t).select(sup_t.id)
             .where(sup_t.name == P())
             .where(sup_t.company_id == P()))
        existing = conn.execute(q.get_sql(), (name, company_id)).fetchone()
        if existing:
            skipped += 1
            continue

        supplier_id = str(uuid.uuid4())
        try:
            naming = get_next_name(conn, "supplier", company_id=company_id)
        except ValueError:
            naming = None
        cell = (raw_currencies[index] or "").strip() if index < len(raw_currencies) else ""
        currency = row.get("default_currency") if cell else company_currency
        q = (Q.into(sup_t)
             .columns("id", "name", "naming_series", "supplier_type",
                      "default_currency", "email", "phone",
                      "tax_id", "company_id")
             .insert(P(), P(), P(), P(), P(), P(), P(), P(), P()))
        conn.execute(q.get_sql(),
            (supplier_id, name, naming,
             normalised_types[index],
             currency,
             row.get("email"), row.get("phone"), row.get("tax_id"),
             company_id))
        imported += 1

    conn.commit()
    ok({"imported": imported, "skipped": skipped, "total_rows": len(rows)})


# ---------------------------------------------------------------------------
# close-purchase-order
# ---------------------------------------------------------------------------

def close_purchase_order(conn, args):
    """Close a partially-received PO. Prevents further receipts/invoices but
    preserves existing child documents."""
    if not args.purchase_order_id:
        err("--purchase-order-id is required")

    po_t = Table("purchase_order")
    q = Q.from_(po_t).select(po_t.star).where(po_t.id == P())
    po = conn.execute(q.get_sql(), (args.purchase_order_id,)).fetchone()
    if not po:
        err(f"Purchase order {args.purchase_order_id} not found")
    if po["status"] in ("draft", "cancelled", "closed"):
        err(f"Cannot close: purchase order is '{po['status']}'")

    close_reason = args.reason or None
    closed_by = args.closed_by or None

    q = (Q.update(po_t)
         .set(po_t.status, ValueWrapper("closed"))
         .set("close_reason", P())
         .set("closed_by", P())
         .set(po_t.updated_at, now())
         .where(po_t.id == P()))
    conn.execute(q.get_sql(), (close_reason, closed_by, args.purchase_order_id))

    audit(conn, "erpclaw-buying", "close-purchase-order", "purchase_order",
          args.purchase_order_id,
          new_values={"status": "closed", "close_reason": close_reason,
                      "closed_by": closed_by})
    conn.commit()
    ok({"purchase_order_id": args.purchase_order_id, "doc_status": "closed",
        "close_reason": close_reason, "closed_by": closed_by})


# ---------------------------------------------------------------------------
# Date helpers for recurring bills
# ---------------------------------------------------------------------------

def _add_months(d: date_type, months: int) -> date_type:
    """Add months to a date, clamping to last day of month if needed."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date_type(year, month, day)


def _next_bill_date(current_date_str: str, frequency: str) -> str:
    """Calculate next bill date based on frequency."""
    parts = current_date_str.split("-")
    d = date_type(int(parts[0]), int(parts[1]), int(parts[2]))

    if frequency == "weekly":
        d += timedelta(days=7)
    elif frequency == "monthly":
        d = _add_months(d, 1)
    elif frequency == "quarterly":
        d = _add_months(d, 3)
    elif frequency == "semi_annually":
        d = _add_months(d, 6)
    elif frequency == "annually":
        d = _add_months(d, 12)
    else:
        d = _add_months(d, 1)

    return d.isoformat()


# ---------------------------------------------------------------------------
# add-blanket-po
# ---------------------------------------------------------------------------

def add_blanket_po(conn, args):
    """Create a blanket purchase order (framework agreement with a supplier)."""
    if not args.supplier_id:
        err("--supplier-id is required")
    if not args.items:
        err("--items is required (JSON array)")
    if not args.company_id:
        err("--company-id is required")
    if not args.valid_from:
        err("--valid-from is required")
    if not args.valid_to:
        err("--valid-to is required")

    # Validate supplier
    sup_t = Table("supplier")
    sq = (Q.from_(sup_t).select(sup_t.star)
          .where((sup_t.id == P()) | (sup_t.name == P())))
    supplier = conn.execute(sq.get_sql(),
        (args.supplier_id, args.supplier_id)).fetchone()
    if not supplier:
        err(f"Supplier {args.supplier_id} not found")
    if supplier["status"] != "active":
        err(f"Supplier {supplier['name']} is {supplier['status']}")
    supplier_id = supplier["id"]

    company_t = Table("company")
    cq = Q.from_(company_t).select(company_t.id).where(company_t.id == P())
    if not conn.execute(cq.get_sql(), (args.company_id,)).fetchone():
        err(f"Company {args.company_id} not found")

    if args.valid_to <= args.valid_from:
        err("--valid-to must be after --valid-from")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    bo_id = str(uuid.uuid4())
    total_qty = Decimal("0")

    # Insert parent blanket_order
    bo_ins = (Q.into(_t_blanket_order)
              .columns("id", "supplier_id", "blanket_order_type",
                        "valid_from", "valid_to", "status", "company_id")
              .insert(P(), P(), ValueWrapper("buying"),
                      P(), P(), ValueWrapper("draft"), P()))
    conn.execute(bo_ins.get_sql(),
        (bo_id, supplier_id, args.valid_from, args.valid_to, args.company_id))

    # Insert child items
    boi_ins = (Q.into(_t_blanket_order_item)
               .columns("id", "blanket_order_id", "item_id", "quantity",
                         "uom", "rate", "amount")
               .insert(P(), P(), P(), P(), P(), P(), P()))
    boi_ins_sql = boi_ins.get_sql()
    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        qty = to_decimal(item.get("qty", "0"))
        if qty <= 0:
            err(f"Item {i}: qty must be > 0")
        rate = to_decimal(item.get("rate", "0"))
        if rate <= 0:
            err(f"Item {i}: rate must be > 0")
        amount = round_currency(qty * rate)
        total_qty += qty

        conn.execute(boi_ins_sql,
            (str(uuid.uuid4()), bo_id, item_id, str(round_currency(qty)),
             item.get("uom"), str(round_currency(rate)), str(amount)))

    # Update totals
    uq = (Q.update(_t_blanket_order)
          .set(_t_blanket_order.total_qty, P())
          .where(_t_blanket_order.id == P()))
    conn.execute(uq.get_sql(), (str(round_currency(total_qty)), bo_id))

    audit(conn, "erpclaw-buying", "add-blanket-po", "blanket_order", bo_id,
           new_values={"supplier_id": supplier_id, "total_qty": str(total_qty)})
    conn.commit()
    ok({"blanket_order_id": bo_id, "supplier_id": supplier_id,
         "total_qty": str(round_currency(total_qty)),
         "valid_from": args.valid_from, "valid_to": args.valid_to})


# ---------------------------------------------------------------------------
# submit-blanket-po
# ---------------------------------------------------------------------------

def submit_blanket_po(conn, args):
    """Activate a blanket purchase order."""
    if not args.blanket_order_id:
        err("--blanket-order-id is required")

    boq = (Q.from_(_t_blanket_order).select(_t_blanket_order.star)
           .where(_t_blanket_order.id == P()))
    bo = conn.execute(boq.get_sql(), (args.blanket_order_id,)).fetchone()
    if not bo:
        err(f"Blanket order {args.blanket_order_id} not found")
    if bo["status"] != "draft":
        err(f"Cannot submit: blanket order is '{bo['status']}' (must be 'draft')")
    if bo["blanket_order_type"] != "buying":
        err("This blanket order is not a buying type")

    uq = (Q.update(_t_blanket_order)
          .set(_t_blanket_order.status, ValueWrapper("active"))
          .set(_t_blanket_order.updated_at, now())
          .where(_t_blanket_order.id == P()))
    conn.execute(uq.get_sql(), (args.blanket_order_id,))

    audit(conn, "erpclaw-buying", "submit-blanket-po", "blanket_order",
           args.blanket_order_id, new_values={"status": "active"})
    conn.commit()
    ok({"blanket_order_id": args.blanket_order_id, "doc_status": "active"})


# ---------------------------------------------------------------------------
# get-blanket-po
# ---------------------------------------------------------------------------

def get_blanket_po(conn, args):
    """Get a blanket purchase order with its items."""
    if not args.blanket_order_id:
        err("--blanket-order-id is required")

    boq = (Q.from_(_t_blanket_order).select(_t_blanket_order.star)
           .where(_t_blanket_order.id == P()))
    bo = conn.execute(boq.get_sql(), (args.blanket_order_id,)).fetchone()
    if not bo:
        err(f"Blanket order {args.blanket_order_id} not found")

    data = row_to_dict(bo)

    # Fetch items
    boiq = (Q.from_(_t_blanket_order_item).select(_t_blanket_order_item.star)
            .where(_t_blanket_order_item.blanket_order_id == P()))
    items = conn.execute(boiq.get_sql(), (args.blanket_order_id,)).fetchall()
    data["items"] = [row_to_dict(r) for r in items]

    ok(data)


# ---------------------------------------------------------------------------
# list-blanket-pos
# ---------------------------------------------------------------------------

def list_blanket_pos(conn, args):
    """List blanket purchase orders."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))
    bo = _t_blanket_order.as_("bo")
    params = []
    crit = (bo.blanket_order_type == ValueWrapper("buying"))

    crit = Criterion.all([crit, bo.company_id == P()])
    params.append(company_id)
    if args.supplier_id:
        crit = Criterion.all([crit, bo.supplier_id == P()])
        params.append(args.supplier_id)
    if args.blanket_status:
        crit = Criterion.all([crit, bo.status == P()])
        params.append(args.blanket_status)

    count_q = Q.from_(bo).select(fn.Count("*")).where(crit)
    total_count = conn.execute(count_q.get_sql(), params).fetchone()[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0

    list_q = (Q.from_(bo).select(bo.star)
              .where(crit)
              .orderby(bo.created_at, order=Order.desc)
              .limit(P()).offset(P()))
    rows = conn.execute(list_q.get_sql(), params + [limit, offset]).fetchall()

    ok({"blanket_orders": [row_to_dict(r) for r in rows],
         "total_count": total_count, "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# create-po-from-blanket
# ---------------------------------------------------------------------------

def create_po_from_blanket(conn, args):
    """Create a purchase order drawing down from an active blanket PO."""
    if not args.blanket_order_id:
        err("--blanket-order-id is required")
    if not args.items:
        err("--items is required (JSON array)")

    # Validate blanket order
    boq = (Q.from_(_t_blanket_order).select(_t_blanket_order.star)
           .where(_t_blanket_order.id == P()))
    bo = conn.execute(boq.get_sql(), (args.blanket_order_id,)).fetchone()
    if not bo:
        err(f"Blanket order {args.blanket_order_id} not found")
    if bo["status"] != "active":
        err(f"Cannot create PO: blanket order is '{bo['status']}' (must be 'active')")
    if bo["blanket_order_type"] != "buying":
        err("This blanket order is not a buying type")

    # Check expiry
    today = _today()
    if bo["valid_to"] < today:
        err(f"Blanket order expired on {bo['valid_to']}")

    supplier_id = bo["supplier_id"]
    company_id = bo["company_id"]

    # Fetch blanket items
    boiq = (Q.from_(_t_blanket_order_item).select(_t_blanket_order_item.star)
            .where(_t_blanket_order_item.blanket_order_id == P()))
    bo_items = conn.execute(boiq.get_sql(), (args.blanket_order_id,)).fetchall()
    bo_items_map = {}
    for bi in bo_items:
        bid = row_to_dict(bi)
        bo_items_map[bid["item_id"]] = bid

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    dims_given = _dimension_input(args)
    dims_obj = dims_given if dims_given is not None else {}
    _validate_dims_before_write(conn, dims_obj)
    dims_text = dimensions_json_text(dims_obj)

    po_id = str(uuid.uuid4())
    posting_date = args.posting_date or today
    total_amount = Decimal("0")
    po_item_rows = []
    blanket_updates = []

    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        qty = to_decimal(item.get("qty", "0"))
        if qty <= 0:
            err(f"Item {i}: qty must be > 0")

        if item_id not in bo_items_map:
            err(f"Item {i}: item {item_id} not in blanket order")

        bi = bo_items_map[item_id]
        max_qty = to_decimal(bi["quantity"])
        ordered = to_decimal(bi["ordered_qty"])
        remaining = max_qty - ordered

        if qty > remaining:
            err(f"Item {i}: requested qty {qty} exceeds remaining blanket qty {remaining}")

        rate = to_decimal(item.get("rate") or bi["rate"])
        amount = round_currency(qty * rate)
        total_amount += amount

        po_item_rows.append((
            str(uuid.uuid4()), po_id, item_id, str(round_currency(qty)),
            item.get("uom") or bi.get("uom"), str(round_currency(rate)),
            str(amount), "0", str(amount),
            item.get("warehouse_id"), item.get("required_date"),
        ))
        blanket_updates.append((bi["id"], str(round_currency(ordered + qty))))

    total_amount = round_currency(total_amount)
    tax_amount, _ = _calculate_tax(conn, args.tax_template_id, total_amount)
    grand_total = round_currency(total_amount + tax_amount)

    # Insert PO
    po_t = Table("purchase_order")
    q = (Q.into(po_t)
         .columns("id", "supplier_id", "order_date", "total_amount",
                  "tax_amount", "grand_total", "tax_template_id", "status",
                  "company_id", "dimensions_json")
         .insert(P(), P(), P(), P(), P(), P(), P(), ValueWrapper("draft"), P(), P()))
    conn.execute(q.get_sql(),
        (po_id, supplier_id, posting_date,
         str(total_amount), str(round_currency(tax_amount)),
         str(grand_total), args.tax_template_id, company_id, dims_text))

    # Insert PO items
    poi_t = Table("purchase_order_item")
    poi_q = (Q.into(poi_t)
             .columns("id", "purchase_order_id", "item_id", "quantity", "uom",
                      "rate", "amount", "discount_percentage", "net_amount",
                      "warehouse_id", "required_date")
             .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P()))
    poi_sql = poi_q.get_sql()
    for row_params in po_item_rows:
        conn.execute(poi_sql, row_params)

    # Update blanket order item ordered_qty
    boi_uq = (Q.update(_t_blanket_order_item)
              .set(_t_blanket_order_item.ordered_qty, P())
              .where(_t_blanket_order_item.id == P()))
    boi_uq_sql = boi_uq.get_sql()
    for boi_id, new_ordered in blanket_updates:
        conn.execute(boi_uq_sql, (new_ordered, boi_id))

    # Update blanket order total ordered_qty
    ordered_sum = conn.execute(
        """SELECT COALESCE(decimal_sum(ordered_qty), '0') as total
           FROM blanket_order_item WHERE blanket_order_id = ?""",
        (args.blanket_order_id,)).fetchone()["total"]
    bo_uq = (Q.update(_t_blanket_order)
             .set(_t_blanket_order.ordered_qty, P())
             .set(_t_blanket_order.updated_at, now())
             .where(_t_blanket_order.id == P()))
    conn.execute(bo_uq.get_sql(), (ordered_sum, args.blanket_order_id))

    audit(conn, "erpclaw-buying", "create-po-from-blanket", "purchase_order", po_id,
           new_values={"blanket_order_id": args.blanket_order_id,
                       "grand_total": str(grand_total)})
    conn.commit()
    ok({"purchase_order_id": po_id,
         "blanket_order_id": args.blanket_order_id,
         "total_amount": str(total_amount),
         "grand_total": str(grand_total)})


# ---------------------------------------------------------------------------
# create-po-from-so (Back-to-Back: SO -> PO)
# ---------------------------------------------------------------------------

def create_po_from_so(conn, args):
    """Create purchase orders from a sales order (back-to-back).

    For each SO item, find the default supplier via item_supplier table,
    create one PO per supplier with the matching items.
    """
    if not args.sales_order_id:
        err("--sales-order-id is required")

    # Validate SO
    so_t = _t_sales_order
    soq = Q.from_(so_t).select(so_t.star).where(so_t.id == P())
    so = conn.execute(soq.get_sql(), (args.sales_order_id,)).fetchone()
    if not so:
        err(f"Sales order {args.sales_order_id} not found")
    if so["status"] not in ("draft", "confirmed"):
        err(f"Cannot create PO: sales order is '{so['status']}' (must be 'draft' or 'confirmed')")

    company_id = so["company_id"]

    # Fetch SO items
    soi_t = _t_sales_order_item
    soiq = Q.from_(soi_t).select(soi_t.star).where(soi_t.sales_order_id == P())
    so_items = conn.execute(soiq.get_sql(), (args.sales_order_id,)).fetchall()
    if not so_items:
        err("Sales order has no items")

    # Group items by supplier
    supplier_items = {}  # supplier_id -> [(so_item_dict, supplier_row)]
    items_without_supplier = []

    for soi_row in so_items:
        soi = row_to_dict(soi_row)
        item_id = soi["item_id"]

        # Find default supplier for this item (lowest priority = highest preference)
        isq = (Q.from_(_t_item_supplier).select(_t_item_supplier.star)
               .where(_t_item_supplier.item_id == P())
               .orderby(_t_item_supplier.priority)
               .limit(1))
        item_sup = conn.execute(isq.get_sql(), (item_id,)).fetchone()

        if not item_sup:
            items_without_supplier.append(item_id)
            continue

        sup_id = item_sup["supplier_id"]
        if sup_id not in supplier_items:
            supplier_items[sup_id] = []
        supplier_items[sup_id].append(soi)

    if not supplier_items:
        err("No items have a default supplier configured. "
            "Set up item_supplier mappings first.")

    dims_given = _dimension_input(args)
    if dims_given is not None:
        dims_obj = dims_given
        dims_text = dimensions_json_text(dims_given)
    else:
        parent_text = so["dimensions_json"] if so["dimensions_json"] else "{}"
        dims_text = parent_text
        try:
            dims_obj = json.loads(parent_text)
        except (ValueError, TypeError):
            dims_obj = {}
        if not isinstance(dims_obj, dict):
            dims_obj = {}
    _validate_dims_before_write(conn, dims_obj)

    # Create one PO per supplier
    pos_created = []
    posting_date = args.posting_date or _today()

    for sup_id, soi_list in supplier_items.items():
        # Validate supplier is active
        sup_t = Table("supplier")
        sq = Q.from_(sup_t).select(sup_t.star).where(sup_t.id == P())
        supplier = conn.execute(sq.get_sql(), (sup_id,)).fetchone()
        if not supplier or supplier["status"] != "active":
            continue

        po_id = str(uuid.uuid4())
        total_amount = Decimal("0")
        po_item_rows = []

        for soi in soi_list:
            qty = to_decimal(soi["quantity"])
            rate = to_decimal(soi["rate"])
            amount = round_currency(qty * rate)
            total_amount += amount

            po_item_rows.append((
                str(uuid.uuid4()), po_id, soi["item_id"],
                str(round_currency(qty)), soi.get("uom"),
                str(round_currency(rate)), str(amount),
                soi.get("discount_percentage", "0"),
                str(round_currency(amount)),
                soi.get("warehouse_id"), None,
            ))

        total_amount = round_currency(total_amount)
        tax_amount, _ = _calculate_tax(conn, args.tax_template_id, total_amount)
        grand_total = round_currency(total_amount + tax_amount)

        # Insert PO
        po_t = Table("purchase_order")
        q = (Q.into(po_t)
             .columns("id", "supplier_id", "order_date", "total_amount",
                      "tax_amount", "grand_total", "tax_template_id", "status",
                      "company_id", "dimensions_json")
             .insert(P(), P(), P(), P(), P(), P(), P(), ValueWrapper("draft"), P(), P()))
        conn.execute(q.get_sql(),
            (po_id, sup_id, posting_date,
             str(total_amount), str(round_currency(tax_amount)),
             str(grand_total), args.tax_template_id, company_id, dims_text))

        # Insert PO items
        poi_t = Table("purchase_order_item")
        poi_q = (Q.into(poi_t)
                 .columns("id", "purchase_order_id", "item_id", "quantity", "uom",
                          "rate", "amount", "discount_percentage", "net_amount",
                          "warehouse_id", "required_date")
                 .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P()))
        poi_sql = poi_q.get_sql()
        for row_params in po_item_rows:
            conn.execute(poi_sql, row_params)

        pos_created.append({
            "purchase_order_id": po_id,
            "supplier_id": sup_id,
            "supplier_name": supplier["name"],
            "grand_total": str(grand_total),
            "items_count": len(po_item_rows),
        })

    audit(conn, "erpclaw-buying", "create-po-from-so", "sales_order",
           args.sales_order_id,
           new_values={"pos_created": len(pos_created)})
    conn.commit()
    ok({"sales_order_id": args.sales_order_id,
         "purchase_orders_created": len(pos_created),
         "purchase_orders": pos_created,
         "items_without_supplier": items_without_supplier})


# ---------------------------------------------------------------------------
# create-po-from-material-request (WS2/D2: MR -> PO)
# ---------------------------------------------------------------------------

def create_po_from_material_request(conn, args):
    """Create a draft purchase order from a submitted material request.

    Copies each line's remaining unordered quantity (quantity - ordered_qty)
    onto a new draft PO for --supplier-id, bumps ordered_qty on the consumed
    lines, and rolls the parent status to partially_ordered/ordered.

    Optional --items JSON = per-line overrides, keyed by
    material_request_item_id or item_id:
      {"item_id": ..., "qty": ..., "rate": ..., "warehouse_id": ...,
       "uom": ..., "required_date": ..., "discount_percentage": ...,
       "discount_amount": ...}
    An override qty of 0 skips the line (partial ordering — a later call
    orders the remainder). Rate resolution mirrors add-purchase-order's
    rate-must-be-positive contract: explicit override, else the item's
    last_purchase_rate, else its standard_rate, else refuse.
    """
    if not args.material_request_id:
        err("--material-request-id is required")
    if not args.supplier_id:
        err("--supplier-id is required")

    mr_t = Table("material_request")
    q = Q.from_(mr_t).select(mr_t.star).where(mr_t.id == P())
    mr = conn.execute(q.get_sql(), (args.material_request_id,)).fetchone()
    if not mr:
        err(f"Material request {args.material_request_id} not found")
    if mr["status"] == "draft":
        err("Cannot create PO: material request is 'draft' (must be submitted first)",
            suggestion="Run submit-material-request, then retry.")
    if mr["status"] not in ("submitted", "partially_ordered"):
        err(f"Cannot create PO: material request is '{mr['status']}' "
            "(must be 'submitted' or 'partially_ordered')")
    if mr["request_type"] != "purchase":
        err(f"Cannot create PO: request type is '{mr['request_type']}' "
            "(only purchase-type material requests convert to purchase orders)")

    # Supplier — mirror add-purchase-order (id-or-name lookup, must be active)
    sup_t = Table("supplier")
    q = (Q.from_(sup_t).select(sup_t.star)
         .where((sup_t.id == P()) | (sup_t.name == P())))
    supplier = conn.execute(q.get_sql(),
                            (args.supplier_id, args.supplier_id)).fetchone()
    if not supplier:
        err(f"Supplier {args.supplier_id} not found")
    supplier_id = supplier["id"]
    if supplier["status"] != "active":
        err(f"Supplier {supplier['name']} is {supplier['status']}")

    mri_t = Table("material_request_item")
    q = (Q.from_(mri_t).select(mri_t.star)
         .where(mri_t.material_request_id == P())
         .orderby(line_order(mri_t)))
    mr_items = conn.execute(q.get_sql(), (args.material_request_id,)).fetchall()
    if not mr_items:
        err("Material request has no items")

    # Optional per-line overrides
    overrides_by_line = {}
    overrides_by_item = {}
    if args.items:
        overrides = _parse_json_arg(args.items, "items")
        if not isinstance(overrides, list):
            err("--items must be a JSON array of per-line overrides")
        for i, ov in enumerate(overrides):
            if not isinstance(ov, dict):
                err(f"Override {i}: must be a JSON object")
            if ov.get("material_request_item_id"):
                overrides_by_line[ov["material_request_item_id"]] = ov
            elif ov.get("item_id"):
                if ov["item_id"] in overrides_by_item:
                    err(f"Override {i}: duplicate override for item {ov['item_id']}")
                overrides_by_item[ov["item_id"]] = ov
            else:
                err(f"Override {i}: material_request_item_id or item_id is required")

    known_line_ids = {r["id"] for r in mr_items}
    for line_id in overrides_by_line:
        if line_id not in known_line_ids:
            err(f"Override line {line_id} is not on this material request")
    mr_item_ids = [r["item_id"] for r in mr_items]
    for item_id in overrides_by_item:
        if item_id not in mr_item_ids:
            err(f"Override item {item_id} is not on this material request")
        if mr_item_ids.count(item_id) > 1:
            err(f"Item {item_id} appears on multiple request lines — "
                "key the override by material_request_item_id instead")

    item_t = Table("item")
    item_sql = (Q.from_(item_t).select(item_t.star)
                .where(item_t.id == P())).get_sql()

    po_id = str(uuid.uuid4())
    posting_date = args.posting_date or _today()
    total_amount = Decimal("0")
    po_item_rows = []
    line_discounts = []
    line_updates = []          # (mri_id, new ordered_qty TEXT)
    ordered_lines = []         # response detail
    total_requested = Decimal("0")
    total_ordered_after = Decimal("0")

    for mri_row in mr_items:
        line = row_to_dict(mri_row)
        requested = to_decimal(str(line["quantity"]))
        already = to_decimal(str(line["ordered_qty"] or "0"))
        remaining = requested - already
        total_requested += requested

        ov = (overrides_by_line.get(line["id"])
              or overrides_by_item.get(line["item_id"]) or {})
        qty = to_decimal(str(ov["qty"])) if "qty" in ov else remaining
        if qty < 0:
            err(f"Item {line['item_id']}: qty must be >= 0")
        if qty > remaining:
            err(f"Item {line['item_id']}: ordering {qty} would exceed the "
                f"remaining unordered quantity {remaining} "
                f"(requested {requested}, already ordered {already})")
        if qty == 0:
            total_ordered_after += already
            continue

        item = conn.execute(item_sql, (line["item_id"],)).fetchone()
        if not item:
            err(f"Item {line['item_id']} not found")

        rate = None
        if "rate" in ov:
            rate = to_decimal(str(ov["rate"]))
        else:
            for source in ("last_purchase_rate", "standard_rate"):
                candidate = to_decimal(str(item[source] or "0"))
                if candidate > 0:
                    rate = candidate
                    break
        if rate is None or rate <= 0:
            err(f"Item {line['item_id']}: rate must be > 0 — the item has no "
                "last purchase rate or standard rate; pass a rate override "
                "in --items")

        amount = round_currency(qty * rate)
        stored_pct, net_amount, line_discount = _order_line_net(
            line["item_id"], amount, ov)
        total_amount += net_amount
        line_discounts.append(str(line_discount))

        po_item_rows.append((
            str(uuid.uuid4()), po_id, line["item_id"],
            str(round_currency(qty)),
            ov.get("uom") or line["uom"] or item["stock_uom"],
            str(round_currency(rate)), str(amount),
            str(stored_pct), str(net_amount),
            ov.get("warehouse_id") or line["warehouse_id"],
            ov.get("required_date") or line["required_date"] or mr["required_date"],
        ))
        new_ordered = already + qty
        line_updates.append((line["id"], str(round_currency(new_ordered))))
        total_ordered_after += new_ordered
        ordered_lines.append({
            "material_request_item_id": line["id"],
            "item_id": line["item_id"],
            "quantity": str(round_currency(qty)),
            "rate": str(round_currency(rate)),
            "net_amount": str(net_amount),
        })

    if not po_item_rows:
        err("Nothing to order: every line is either fully ordered "
            "or skipped by overrides")

    dims_given = _dimension_input(args)
    dims_obj = dims_given if dims_given is not None else {}
    _validate_dims_before_write(conn, dims_obj)
    dims_text = dimensions_json_text(dims_obj)

    tax_amount, _ = _calculate_tax(conn, args.tax_template_id, total_amount)
    grand_total = round_currency(total_amount + tax_amount)

    # Insert PO parent + items (same shape as add-purchase-order)
    po_t = Table("purchase_order")
    q = (Q.into(po_t)
         .columns("id", "supplier_id", "order_date", "total_amount",
                  "tax_amount", "grand_total", "tax_template_id", "status",
                  "company_id", "dimensions_json")
         .insert(P(), P(), P(), P(), P(), P(), P(), ValueWrapper("draft"), P(), P()))
    conn.execute(q.get_sql(),
        (po_id, supplier_id, posting_date,
         str(round_currency(total_amount)), str(round_currency(tax_amount)),
         str(grand_total), args.tax_template_id, mr["company_id"], dims_text))

    poi_t = Table("purchase_order_item")
    poi_q = (Q.into(poi_t)
             .columns("id", "purchase_order_id", "item_id", "quantity", "uom",
                      "rate", "amount", "discount_percentage", "net_amount",
                      "warehouse_id", "required_date")
             .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P()))
    poi_sql = poi_q.get_sql()
    for row_params in po_item_rows:
        conn.execute(poi_sql, row_params)

    # Consume the request lines
    upd_sql = (Q.update(mri_t)
               .set(mri_t.ordered_qty, P())
               .where(mri_t.id == P())).get_sql()
    for mri_id, new_qty in line_updates:
        conn.execute(upd_sql, (new_qty, mri_id))

    # Roll up parent status from the post-update totals
    new_status = ("ordered" if total_ordered_after >= total_requested
                  else "partially_ordered")
    q = (Q.update(mr_t)
         .set(mr_t.status, P())
         .set(mr_t.updated_at, now())
         .where(mr_t.id == P()))
    conn.execute(q.get_sql(), (new_status, args.material_request_id))

    audit(conn, "erpclaw-buying", "create-po-from-material-request",
           "purchase_order", po_id,
           new_values={"material_request_id": args.material_request_id,
                       "supplier_id": supplier_id,
                       "grand_total": str(grand_total),
                       "material_request_status": new_status,
                       "line_discounts": line_discounts})
    conn.commit()
    ok({"purchase_order_id": po_id,
         "material_request_id": args.material_request_id,
         "supplier_id": supplier_id,
         "items_ordered": len(po_item_rows),
         "items": ordered_lines,
         "total_amount": str(round_currency(total_amount)),
         "tax_amount": str(round_currency(tax_amount)),
         "grand_total": str(grand_total),
         "material_request_status": new_status})


# ---------------------------------------------------------------------------
# add-recurring-bill-template
# ---------------------------------------------------------------------------

def add_recurring_bill_template(conn, args):
    """Create a recurring bill (AP invoice) template."""
    if not args.supplier_id:
        err("--supplier-id is required")
    if not args.items:
        err("--items is required (JSON array)")
    if not args.frequency:
        err("--frequency is required")
    if not args.start_date:
        err("--start-date is required")
    if not args.company_id:
        err("--company-id is required")

    if args.frequency not in VALID_FREQUENCIES:
        err(f"--frequency must be one of: {', '.join(VALID_FREQUENCIES)}")

    company_t = Table("company")
    cq = Q.from_(company_t).select(company_t.id).where(company_t.id == P())
    if not conn.execute(cq.get_sql(), (args.company_id,)).fetchone():
        err(f"Company {args.company_id} not found")

    # Validate supplier
    sup_t = Table("supplier")
    sq = (Q.from_(sup_t).select(sup_t.id, sup_t.company_id)
          .where((sup_t.id == P())
                 | ((sup_t.name == P()) & (sup_t.company_id == P())))
          .where(sup_t.status == ValueWrapper("active")))
    sup = conn.execute(sq.get_sql(),
        (args.supplier_id, args.supplier_id, args.company_id)).fetchone()
    if not sup:
        err(f"Active supplier {args.supplier_id} not found")
    supplier_id = sup["id"]
    if sup["company_id"] != args.company_id:
        err(f"Supplier {supplier_id} belongs to another company")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    auto_submit = 1 if args.auto_submit else 0

    rt_id = str(uuid.uuid4())

    # Insert parent template
    rt_ins = (Q.into(_t_recurring_bill_template)
              .columns("id", "supplier_id", "frequency", "start_date",
                        "end_date", "next_bill_date", "tax_template_id",
                        "auto_submit", "status", "company_id")
              .insert(P(), P(), P(), P(), P(), P(), P(), P(),
                      ValueWrapper("draft"), P()))
    conn.execute(rt_ins.get_sql(),
        (rt_id, supplier_id, args.frequency, args.start_date,
         args.end_date, args.start_date, args.tax_template_id,
         auto_submit, args.company_id))

    # Insert child items
    rti_ins = (Q.into(_t_recurring_bill_template_item)
               .columns("id", "template_id", "item_id", "quantity", "uom",
                         "rate", "amount")
               .insert(P(), P(), P(), P(), P(), P(), P()))
    rti_ins_sql = rti_ins.get_sql()
    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        qty = to_decimal(item.get("qty", "0"))
        rate = to_decimal(item.get("rate", "0"))
        amount = round_currency(qty * rate)

        conn.execute(rti_ins_sql,
            (str(uuid.uuid4()), rt_id, item_id, str(round_currency(qty)),
             item.get("uom"), str(round_currency(rate)), str(amount)))

    audit(conn, "erpclaw-buying", "add-recurring-bill-template",
           "recurring_bill_template", rt_id,
           new_values={"supplier_id": supplier_id, "frequency": args.frequency})
    conn.commit()
    ok({"template_id": rt_id, "frequency": args.frequency,
         "start_date": args.start_date, "next_bill_date": args.start_date})


# ---------------------------------------------------------------------------
# update-recurring-bill-template
# ---------------------------------------------------------------------------

def update_recurring_bill_template(conn, args):
    """Update a recurring bill (AP invoice) template.

    Closes the draft-then-activate lifecycle: a draft template never
    generates, so activate it (status active) before generate-recurring-bills
    can pick it up; pause or cancel it to stop generation. Mirrors selling's
    update_recurring_template field by field. Every refusal comes before the
    first UPDATE or DELETE.
    """
    if not args.template_id:
        err("--template-id is required")

    rtq = (Q.from_(_t_recurring_bill_template).select(_t_recurring_bill_template.star)
           .where(_t_recurring_bill_template.id == P()))
    rt = conn.execute(rtq.get_sql(), (args.template_id,)).fetchone()
    if not rt:
        err(f"Recurring bill template {args.template_id} not found")
    old_status = row_to_dict(rt).get("status")
    if old_status == "cancelled":
        err(f"Recurring bill template {args.template_id} is 'cancelled' and cannot be changed")

    if args.frequency is not None:
        if args.frequency not in VALID_FREQUENCIES:
            err(f"--frequency must be one of: {', '.join(VALID_FREQUENCIES)}")

    if args.template_status is not None:
        if args.template_status not in ("active", "paused", "cancelled"):
            err("--template-status must be 'active', 'paused', or 'cancelled'")

    parsed_items = None
    if args.items:
        parsed_items = _parse_json_arg(args.items, "items")
        if not parsed_items or not isinstance(parsed_items, list):
            err("--items must be a non-empty JSON array")
        for i, item in enumerate(parsed_items):
            if not isinstance(item, dict) or not item.get("item_id"):
                err(f"Item {i}: item_id is required")

    updated_fields = []
    if args.frequency is not None:
        updated_fields.append("frequency")
    if args.template_status is not None:
        updated_fields.append("status")
    if args.items:
        updated_fields.append("items")

    if not updated_fields:
        err("No fields to update")

    if args.frequency is not None:
        uq = (Q.update(_t_recurring_bill_template)
              .set("frequency", P())
              .set("updated_at", now())
              .where(_t_recurring_bill_template.id == P()))
        conn.execute(uq.get_sql(), (args.frequency, args.template_id))

    if args.template_status is not None:
        uq2 = (Q.update(_t_recurring_bill_template)
               .set("status", P())
               .set("updated_at", now())
               .where(_t_recurring_bill_template.id == P()))
        conn.execute(uq2.get_sql(), (args.template_status, args.template_id))

    if args.items:
        dq = (Q.from_(_t_recurring_bill_template_item).delete()
              .where(_t_recurring_bill_template_item.template_id == P()))
        conn.execute(dq.get_sql(), (args.template_id,))

        rti_ins = (Q.into(_t_recurring_bill_template_item)
                   .columns("id", "template_id", "item_id", "quantity", "uom",
                             "rate", "amount")
                   .insert(P(), P(), P(), P(), P(), P(), P()))
        rti_ins_sql = rti_ins.get_sql()
        for i, item in enumerate(parsed_items):
            item_id = item.get("item_id")
            if not item_id:
                err(f"Item {i}: item_id is required")
            qty = to_decimal(item.get("qty", "0"))
            rate = to_decimal(item.get("rate", "0"))
            amount = round_currency(qty * rate)

            conn.execute(rti_ins_sql,
                (str(uuid.uuid4()), args.template_id, item_id,
                 str(round_currency(qty)), item.get("uom"),
                 str(round_currency(rate)), str(amount)),
            )

    if args.template_status is not None:
        audit(conn, "erpclaw-buying", "update-recurring-bill-template", "recurring_bill_template",
               args.template_id, old_values={"status": old_status},
               new_values={"updated_fields": updated_fields, "status": args.template_status})
    else:
        audit(conn, "erpclaw-buying", "update-recurring-bill-template", "recurring_bill_template",
               args.template_id, new_values={"updated_fields": updated_fields})
    conn.commit()
    ok({"template_id": args.template_id, "updated_fields": updated_fields})


# ---------------------------------------------------------------------------
# list-recurring-bill-templates
# ---------------------------------------------------------------------------

def list_recurring_bill_templates(conn, args):
    """List recurring bill templates."""
    company_id = resolve_scope_company(conn, args.company_id, getattr(args, "company_name", None))
    rt = _t_recurring_bill_template.as_("rt")
    params = [company_id]
    crit = (rt.company_id == P())
    if args.supplier_id:
        cond = rt.supplier_id == P()
        crit = Criterion.all([crit, cond]) if crit else cond
        params.append(args.supplier_id)
    if args.template_status:
        cond = rt.status == P()
        crit = Criterion.all([crit, cond]) if crit else cond
        params.append(args.template_status)

    count_q = Q.from_(rt).select(fn.Count("*"))
    if crit:
        count_q = count_q.where(crit)
    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0

    list_q = (Q.from_(rt).select(rt.star)
              .orderby(rt.next_bill_date)
              .limit(P()).offset(P()))
    if crit:
        list_q = list_q.where(crit)
    rows = conn.execute(list_q.get_sql(), params + [limit, offset]).fetchall()

    ok({"recurring_bill_templates": [row_to_dict(r) for r in rows],
         "total_count": total_count, "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# generate-recurring-bills
# ---------------------------------------------------------------------------

def generate_recurring_bills(conn, args):
    """Cron: auto-generate purchase invoices from due bill templates."""
    if not args.company_id:
        err("--company-id is required")

    as_of_date = args.as_of_date or _today()

    # raw SQL — OR with IS NULL comparison
    templates = conn.execute(
        """SELECT * FROM recurring_bill_template
           WHERE status = 'active'
             AND next_bill_date <= ?
             AND company_id = ?
             AND (end_date IS NULL OR end_date >= ?)""",
        (as_of_date, args.company_id, as_of_date),
    ).fetchall()

    bills_generated = []
    templates_completed = 0
    errors = []

    for tmpl in templates:
        tmpl_dict = row_to_dict(tmpl)
        template_id = tmpl_dict["id"]
        supplier_id = tmpl_dict["supplier_id"]
        company_id = tmpl_dict["company_id"]
        next_date = tmpl_dict["next_bill_date"]
        frequency = tmpl_dict["frequency"]
        auto_submit = tmpl_dict.get("auto_submit", 0)

        try:
            # Fetch template items
            tiq = (Q.from_(_t_recurring_bill_template_item)
                   .select(_t_recurring_bill_template_item.star)
                   .where(_t_recurring_bill_template_item.template_id == P()))
            tmpl_items = conn.execute(tiq.get_sql(), (template_id,)).fetchall()
            if not tmpl_items:
                errors.append({"template_id": template_id,
                               "error": "Template has no items"})
                continue

            # Build invoice items
            total_amount = Decimal("0")
            pi_items_data = []
            for ti in tmpl_items:
                ti_dict = row_to_dict(ti)
                qty = to_decimal(ti_dict["quantity"])
                rate = to_decimal(ti_dict["rate"])
                net = round_currency(qty * rate)
                total_amount += net
                pi_items_data.append({
                    "item_id": ti_dict["item_id"],
                    "qty": str(round_currency(qty)),
                    "uom": ti_dict.get("uom"),
                    "rate": str(round_currency(rate)),
                    "amount": str(net),
                })

            total_amount = round_currency(total_amount)
            tax_amount, _ = _calculate_tax(conn, tmpl_dict.get("tax_template_id"), total_amount)
            grand_total = round_currency(total_amount + tax_amount)

            # Calculate due date (30 days from posting)
            parts = next_date.split("-")
            d = date_type(int(parts[0]), int(parts[1]), int(parts[2]))
            due_date = (d + timedelta(days=30)).isoformat()

            pi_id = str(uuid.uuid4())

            sup_t = Table("supplier")
            supq = Q.from_(sup_t).select(sup_t.star).where(sup_t.id == P())
            supplier_row = conn.execute(supq.get_sql(), (supplier_id,)).fetchone()
            currency = _document_currency(conn, supplier_row, company_id)

            # Create purchase invoice
            pi_t = Table("purchase_invoice")
            pi_ins = (Q.into(pi_t)
                       .columns("id", "supplier_id", "posting_date", "due_date",
                                 "total_amount", "tax_amount", "grand_total",
                                 "outstanding_amount", "tax_template_id",
                                 "status", "update_stock", "company_id",
                                 "currency", "exchange_rate")
                       .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(),
                               ValueWrapper("draft"), 0, P(), P(), P()))
            conn.execute(pi_ins.get_sql(),
                (pi_id, supplier_id, next_date, due_date,
                 str(total_amount), str(tax_amount), str(grand_total),
                 str(grand_total), tmpl_dict.get("tax_template_id"),
                 company_id, currency, "1"))

            pii_t = Table("purchase_invoice_item")
            pii_ins = (Q.into(pii_t)
                        .columns("id", "purchase_invoice_id", "item_id", "quantity",
                                  "uom", "rate", "amount")
                        .insert(P(), P(), P(), P(), P(), P(), P()))
            pii_ins_sql = pii_ins.get_sql()
            for row in pi_items_data:
                conn.execute(pii_ins_sql,
                    (str(uuid.uuid4()), pi_id, row["item_id"], row["qty"],
                     row["uom"], row["rate"], row["amount"]))

            status = "draft"
            naming = None

            # Auto-submit if configured
            if auto_submit:
                submit_resp = _submit_purchase_invoice_in_txn(
                    conn, pi_id,
                    remarks=f"Recurring Bill from template {template_id}")
                naming = submit_resp["naming_series"]
                status = submit_resp["status"]

            # Update template dates
            new_next = _next_bill_date(next_date, frequency)
            uq_rt = (Q.update(_t_recurring_bill_template)
                     .set("last_bill_date", P())
                     .set("next_bill_date", P())
                     .set("updated_at", now())
                     .where(_t_recurring_bill_template.id == P()))
            conn.execute(uq_rt.get_sql(), (next_date, new_next, template_id))

            # Check if template is completed
            template_completed = False
            if tmpl_dict.get("end_date") and new_next > tmpl_dict["end_date"]:
                uq_comp = (Q.update(_t_recurring_bill_template)
                           .set("status", ValueWrapper("completed"))
                           .set("updated_at", now())
                           .where(_t_recurring_bill_template.id == P()))
                conn.execute(uq_comp.get_sql(), (template_id,))
                template_completed = True

            conn.commit()
            if template_completed:
                templates_completed += 1

            bills_generated.append({
                "template_id": template_id,
                "invoice_id": pi_id,
                "naming_series": naming,
                "supplier_id": supplier_id,
                "amount": str(grand_total),
                "status": status,
            })

        except Exception as e:
            conn.rollback()
            errors.append({"template_id": template_id,
                           "error": str(e)})
            continue

    conn.commit()
    ok({"bills_generated": len(bills_generated),
         "templates_processed": len(templates),
         "templates_completed": templates_completed,
         "bills": bills_generated,
         "errors": errors})


# ---------------------------------------------------------------------------
# Buying account helpers (for recurring bills auto-submit)
# ---------------------------------------------------------------------------

def _get_payable_account(conn, company_id: str) -> str | None:
    """Return the default payable account for a company."""
    company_t = Table("company")
    q = (Q.from_(company_t)
         .select(company_t.default_payable_account_id)
         .where(company_t.id == P()))
    company = conn.execute(q.get_sql(), (company_id,)).fetchone()
    if company and company["default_payable_account_id"]:
        return company["default_payable_account_id"]
    acct_t = Table("account")
    q2 = (Q.from_(acct_t)
          .select(acct_t.id)
          .where(acct_t.account_type == ValueWrapper("payable"))
          .where(acct_t.company_id == P())
          .where(acct_t.is_group == 0)
          .limit(1))
    acct = conn.execute(q2.get_sql(), (company_id,)).fetchone()
    return acct["id"] if acct else None


def _get_expense_account(conn, company_id: str) -> str | None:
    """Return the default expense account for a company."""
    company_t = Table("company")
    q = (Q.from_(company_t)
         .select(company_t.default_expense_account_id)
         .where(company_t.id == P()))
    company = conn.execute(q.get_sql(), (company_id,)).fetchone()
    if company and company["default_expense_account_id"]:
        return company["default_expense_account_id"]
    acct_t = Table("account")
    q2 = (Q.from_(acct_t)
          .select(acct_t.id)
          .where(acct_t.root_type == ValueWrapper("expense"))
          .where(acct_t.company_id == P())
          .where(acct_t.is_group == 0)
          .limit(1))
    acct = conn.execute(q2.get_sql(), (company_id,)).fetchone()
    return acct["id"] if acct else None


# ---------------------------------------------------------------------------
# Feature #19: Multi-UOM on PO (Sprint 7)
# ---------------------------------------------------------------------------


def set_item_purchase_uom(conn, args):
    """Set default purchase UOM and conversion factor for an item.

    Required: --item-id, --purchase-uom, --conversion-factor
    Creates/updates a uom_conversion record for the item.
    """
    if not args.item_id:
        err("--item-id is required")
    if not args.purchase_uom:
        err("--purchase-uom is required")
    if not args.conversion_factor:
        err("--conversion-factor is required")

    # Validate item
    item_t = Table("item")
    iq = Q.from_(item_t).select(item_t.star).where(item_t.id == P())
    item = conn.execute(iq.get_sql(), (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")
    item_d = row_to_dict(item)
    stock_uom = item_d.get("stock_uom", "Each")

    conversion_factor = to_decimal(args.conversion_factor)
    if conversion_factor <= 0:
        err("--conversion-factor must be > 0")

    purchase_uom = args.purchase_uom.strip()

    # Check if UOM conversion already exists for this item
    uc_t = Table("uom_conversion")
    existing_q = (Q.from_(uc_t).select(uc_t.id)
                  .where(uc_t.item_id == P())
                  .where(uc_t.from_uom == P())
                  .where(uc_t.to_uom == P()))

    # Look up UOM IDs (from_uom = purchase_uom, to_uom = stock_uom)
    uom_t = Table("uom")
    pq = Q.from_(uom_t).select(uom_t.id).where(uom_t.name == P())
    purchase_uom_row = conn.execute(pq.get_sql(), (purchase_uom,)).fetchone()
    stock_uom_row = conn.execute(pq.get_sql(), (stock_uom,)).fetchone()

    # If UOMs don't exist as IDs in the uom table, use the name strings directly
    purchase_uom_id = purchase_uom_row["id"] if purchase_uom_row else purchase_uom
    stock_uom_id = stock_uom_row["id"] if stock_uom_row else stock_uom

    existing = conn.execute(existing_q.get_sql(),
                            (args.item_id, purchase_uom_id, stock_uom_id)).fetchone()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if existing:
        # Update existing conversion
        conn.execute(
            "UPDATE uom_conversion SET conversion_factor = ? WHERE id = ?",
            (str(round_currency(conversion_factor)), existing["id"])
        )
        conv_id = existing["id"]
        action_taken = "updated"
    else:
        # Create new conversion
        conv_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO uom_conversion (id, from_uom, to_uom, conversion_factor,
               item_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (conv_id, purchase_uom_id, stock_uom_id,
             str(round_currency(conversion_factor)), args.item_id, now)
        )
        action_taken = "created"

    audit(conn, "erpclaw-buying", "set-item-purchase-uom",
          "uom_conversion", conv_id,
          new_values={"item_id": args.item_id,
                      "purchase_uom": purchase_uom,
                      "conversion_factor": str(conversion_factor)})
    conn.commit()

    ok({
        "uom_conversion_id": conv_id,
        "item_id": args.item_id,
        "purchase_uom": purchase_uom,
        "stock_uom": stock_uom,
        "conversion_factor": str(round_currency(conversion_factor)),
        "action": action_taken,
        "message": f"Purchase UOM {action_taken} for item",
    })


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

def _worksheet_object(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys():
        err("Commitment worksheet object is missing required fields")
    if value.keys() - required - set(optional):
        err("Commitment worksheet contains unsupported fields")
    return value


def _worksheet_money(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,15}(?:\.[0-9]{1,2})?", value):
        err(f"{label} must be a nonnegative Decimal string with at most two decimal places")
    return Decimal(value)


def _worksheet_quantity(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        err("Stored commitment quantity is invalid")
    if not amount.is_finite() or amount < 0:
        err("Stored commitment quantity must be finite and nonnegative")
    return amount


def _worksheet_text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value):
        err(f"{label} must be nonempty text of at most 120 characters")
    return value


def _worksheet_row(conn, table_name, row_id, company_id):
    table = Table(table_name)
    row = conn.execute(Q.from_(table).select(table.star).where(table.id == P()).get_sql(), (row_id,)).fetchone()
    if not row or (row["id"] if table_name == "company" else row["company_id"]) != company_id:
        err(f"{table_name} not found in the selected company")
    return dict(row)


def _worksheet_relief(conn, po, line, kind):
    item = Table("item")
    item_row = conn.execute(Q.from_(item).select(item.stock_uom).where(item.id == P()).get_sql(), (line["item_id"],)).fetchone()
    if not item_row:
        err("Commitment order item no longer exists")
    order_uom = line["uom"] or item_row["stock_uom"]
    parent = Table("purchase_" + kind)
    child = Table("purchase_" + kind + "_item")
    statuses = ("submitted",) if kind == "receipt" else ("submitted", "partially_paid", "paid", "overdue")
    q = (Q.from_(child).join(parent).on(child["purchase_" + kind + "_id"] == parent.id)
         .select(child.quantity, child.uom, parent.company_id, parent.purchase_order_id, child.item_id)
         .where(child.purchase_order_item_id == P()).where(parent.status.isin(statuses)))
    if kind == "invoice":
        q = q.where(parent.is_return == 0)
    total = Decimal("0")
    for row in conn.execute(q.get_sql(), (line["id"],)).fetchall():
        if (row["company_id"] != po["company_id"] or row["item_id"] != line["item_id"]
                or row["purchase_order_id"] not in (None, po["id"])):
            err("Commitment relief document has a different company or item")
        if (row["uom"] or item_row["stock_uom"]) != order_uom:
            err("Commitment relief and purchase order must use the same UOM")
        total += _worksheet_quantity(row["quantity"])
    return total


def add_commitment_worksheet(conn, args):
    """Save an unenforced buying commitment calculation, never a reservation."""
    company_id = getattr(args, "company_id", None)
    if not company_id:
        err("--company-id is required")
    company = _worksheet_row(conn, "company", company_id, company_id)
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate commitment worksheet field")
                result[key] = value
            return result
        data = json.loads(getattr(args, "worksheet_json", None), object_pairs_hook=pairs)
    except (TypeError, ValueError):
        err("--worksheet-json must be valid JSON without duplicate fields")
    _worksheet_object(data, {"fund_reference", "award_reference", "fiscal_year_id", "budget_amount", "actual_amount", "requisitions", "purchase_orders"})
    for key in ("fund_reference", "award_reference", "fiscal_year_id"):
        _worksheet_text(data[key], key)
    year = _worksheet_row(conn, "fiscal_year", data["fiscal_year_id"], company_id)
    budget = _worksheet_money(data["budget_amount"], "budget_amount")
    actual = _worksheet_money(data["actual_amount"], "actual_amount")
    if not all(isinstance(data[k], list) and len(data[k]) <= 100 for k in ("requisitions", "purchase_orders")):
        err("requisitions and purchase_orders must be arrays of at most 100 documents")
    if not data["requisitions"] and not data["purchase_orders"]:
        err("Select at least one requisition or purchase order")
    requests, seen_requests = {}, set()
    for selection in data["requisitions"]:
        _worksheet_object(selection, {"material_request_id", "rates"})
        rid = _worksheet_text(selection["material_request_id"], "material_request_id")
        if rid in seen_requests:
            err("Repeated requisition")
        seen_requests.add(rid)
        mr = _worksheet_row(conn, "material_request", rid, company_id)
        if mr["request_type"] != "purchase" or mr["status"] not in ("submitted", "partially_ordered", "ordered"):
            err("Select only submitted purchase requisitions")
        if not year["start_date"] <= mr["created_at"][:10] <= year["end_date"]:
            err("Requisition is outside the selected fiscal year")
        table = Table("material_request_item")
        lines = {r["id"]: dict(r) for r in conn.execute(Q.from_(table).select(table.star).where(table.material_request_id == P()).get_sql(), (rid,)).fetchall()}
        if not isinstance(selection["rates"], list) or not selection["rates"]:
            err("Each requisition requires reviewed rates for every line")
        priced = set()
        for rate in selection["rates"]:
            _worksheet_object(rate, {"material_request_item_id", "unit_rate"})
            lid = _worksheet_text(rate["material_request_item_id"], "material_request_item_id")
            if lid not in lines or lid in priced:
                err("Requisition rate names a missing or repeated line")
            priced.add(lid)
            requests[lid] = {**lines[lid], "unit_rate": _worksheet_money(rate["unit_rate"], "unit_rate"), "linked_quantity": Decimal("0")}
        if priced != lines.keys():
            err("Each requisition requires reviewed rates for every line")
    orders, seen_orders, encumbrance = [], set(), Decimal("0")
    for selection in data["purchase_orders"]:
        _worksheet_object(selection, {"purchase_order_id", "requisition_links"})
        pid = _worksheet_text(selection["purchase_order_id"], "purchase_order_id")
        if pid in seen_orders:
            err("Repeated purchase order")
        seen_orders.add(pid)
        po = _worksheet_row(conn, "purchase_order", pid, company_id)
        if po["status"] == "draft":
            err("Draft purchase orders are not commitments")
        if not year["start_date"] <= po["order_date"] <= year["end_date"]:
            err("Purchase order is outside the selected fiscal year")
        if po["currency"] != company["default_currency"] or Decimal(po["exchange_rate"]) != 1:
            err("Commitment worksheet supports company-currency purchase orders only")
        try:
            dims = json.loads(po["dimensions_json"])
        except (ValueError, TypeError):
            err("Purchase order dimensions are invalid")
        if not isinstance(dims, dict):
            err("Purchase order dimensions are invalid")
        for key in ("fund", "award"):
            if dims.get(key) and dims[key] != data[key + "_reference"]:
                err("Purchase order fund or award differs from worksheet references")
        table = Table("purchase_order_item")
        lines = [dict(r) for r in conn.execute(Q.from_(table).select(table.star).where(table.purchase_order_id == P()).orderby(table.id).get_sql(), (pid,)).fetchall()]
        line_map = {r["id"]: r for r in lines}
        if not lines or not isinstance(selection["requisition_links"], list):
            err("Purchase order requires lines and an explicit requisition_links array")
        linked = set()
        for link in selection["requisition_links"]:
            _worksheet_object(link, {"purchase_order_item_id", "material_request_item_id"})
            lid, rid = link["purchase_order_item_id"], link["material_request_item_id"]
            if not isinstance(lid, str) or not isinstance(rid, str) or lid not in line_map or rid not in requests or lid in linked:
                err("Requisition link names a missing or repeated line")
            linked.add(lid)
            item = Table("item")
            item_row = conn.execute(Q.from_(item).select(item.stock_uom).where(item.id == P()).get_sql(), (requests[rid]["item_id"],)).fetchone()
            if not item_row:
                err("Linked requisition item no longer exists")
            if line_map[lid]["item_id"] != requests[rid]["item_id"] or (line_map[lid]["uom"] or item_row["stock_uom"]) != (requests[rid]["uom"] or item_row["stock_uom"]):
                err("Linked requisition and order must have the same item and UOM")
            requests[rid]["linked_quantity"] += _worksheet_quantity(line_map[lid]["quantity"])
        net = sum((_worksheet_money(r["net_amount"], "stored net_amount") for r in lines), Decimal("0"))
        tax = _worksheet_money(po["tax_amount"], "stored tax_amount")
        grand = _worksheet_money(po["grand_total"], "stored grand_total")
        if net != _worksheet_money(po["total_amount"], "stored total_amount") or grand != net + tax or (net == 0 and tax != 0):
            err("Purchase order totals do not agree with its net lines")
        remainder, order_amount = tax, Decimal("0")
        for index, line in enumerate(lines):
            quantity = _worksheet_quantity(line["quantity"])
            if quantity <= 0:
                err("Purchase order line must have a positive quantity")
            line_net = Decimal(line["net_amount"])
            share = remainder if index == len(lines) - 1 else (min(remainder, round_currency(tax * line_net / net)) if net else Decimal("0"))
            remainder -= share
            if po["status"] in ("cancelled", "closed"):
                remaining = Decimal("0")
            else:
                received = _worksheet_relief(conn, po, line, "receipt")
                invoiced = _worksheet_relief(conn, po, line, "invoice")
                remaining = max(Decimal("0"), quantity - max(received, invoiced))
            amount = round_currency((line_net + share) * remaining / quantity)
            order_amount += amount
        encumbrance += order_amount
        orders.append({"purchase_order_id": pid, "status": po["status"], "remaining_commitment": str(round_currency(order_amount))})
    pre_encumbrance, requisitions = Decimal("0"), []
    for lid, line in sorted(requests.items()):
        quantity, ordered = _worksheet_quantity(line["quantity"]), _worksheet_quantity(line["ordered_qty"])
        if line["linked_quantity"] != ordered or ordered > quantity:
            err("Requisition ordered quantity must match explicitly linked purchase orders")
        amount = round_currency((quantity - ordered) * line["unit_rate"])
        pre_encumbrance += amount
        requisitions.append({"material_request_item_id": lid, "remaining_quantity": str(quantity - ordered), "pre_encumbrance": str(amount)})
    available = round_currency(budget - actual - encumbrance - pre_encumbrance)
    worksheet_id = str(uuid.uuid4())
    result = {"worksheet_id": worksheet_id, "company_id": company_id, "currency": company["default_currency"],
              "fund_reference": data["fund_reference"], "award_reference": data["award_reference"], "fiscal_year_id": data["fiscal_year_id"],
              "calculated_at": datetime.now(timezone.utc).isoformat(), "budget_amount": str(round_currency(budget)), "actual_amount": str(round_currency(actual)),
              "pre_encumbrance": str(round_currency(pre_encumbrance)), "encumbrance": str(round_currency(encumbrance)), "available_balance": str(available),
              "budget_exceeded": available < 0, "enforced": False, "input_basis": "Operator-reviewed budget, actuals, references and requisition links; current selected documents only",
              "requisitions": requisitions, "purchase_orders": orders, "reviewed_input": data}
    try:
        audit(conn, "erpclaw-buying", "add-commitment-worksheet", "commitment_worksheet", worksheet_id, new_values=result)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    ok(result)


def get_commitment_worksheet(conn, args):
    """Read a saved calculation without recalculating or reserving money."""
    company_id = getattr(args, "company_id", None)
    if not company_id or not getattr(args, "worksheet_id", None):
        err("--company-id and --worksheet-id are required")
    _worksheet_row(conn, "company", company_id, company_id)
    table = Table("audit_log")
    q = (Q.from_(table).select(table.new_values).where(table.skill == P()).where(table.action == P())
         .where(table.entity_type == P()).where(table.entity_id == P()))
    rows = conn.execute(q.get_sql(), ("erpclaw-buying", "add-commitment-worksheet", "commitment_worksheet", args.worksheet_id)).fetchall()
    if len(rows) != 1:
        err("Commitment worksheet not found")
    result = json.loads(rows[0]["new_values"])
    if result["company_id"] != company_id:
        err("Commitment worksheet not found in the selected company")
    ok(result)


def _resolve_company_flag(conn, args):
    """Resolve --company (name or id) into args.company_id."""
    if getattr(args, "company_name", None) and not getattr(args, "company_id", None):
        c = Table("company")
        probe = (Q.from_(c).select(c.id).where(c.id == P()))
        if conn.execute(probe.get_sql(), (args.company_name,)).fetchone():
            args.company_id = args.company_name
            return
        args.company_id = resolve_company_id(conn, None, args.company_name)


ACTIONS = {
    "add-commitment-worksheet": add_commitment_worksheet,
    "get-commitment-worksheet": get_commitment_worksheet,
    "add-supplier": add_supplier,
    "update-supplier": update_supplier,
    "get-supplier": get_supplier,
    "list-suppliers": list_suppliers,
    "add-material-request": add_material_request,
    "submit-material-request": submit_material_request,
    "list-material-requests": list_material_requests,
    "get-material-request": get_material_request,
    "create-po-from-material-request": create_po_from_material_request,
    "add-rfq": add_rfq,
    "create-rfq-supplier-request": create_rfq_supplier_request,
    "list-rfq-supplier-requests": list_rfq_supplier_requests,
    "submit-rfq": submit_rfq,
    "list-rfqs": list_rfqs,
    "add-supplier-quotation": add_supplier_quotation,
    "list-supplier-quotations": list_supplier_quotations,
    "compare-supplier-quotations": compare_supplier_quotations,
    "add-purchase-order": add_purchase_order,
    "update-purchase-order": update_purchase_order,
    "get-purchase-order": get_purchase_order,
    "list-purchase-orders": list_purchase_orders,
    "submit-purchase-order": submit_purchase_order,
    "cancel-purchase-order": cancel_purchase_order,
    "close-purchase-order": close_purchase_order,
    "create-purchase-receipt": create_purchase_receipt,
    "get-purchase-receipt": get_purchase_receipt,
    "list-purchase-receipts": list_purchase_receipts,
    "submit-purchase-receipt": submit_purchase_receipt,
    "cancel-purchase-receipt": cancel_purchase_receipt,
    "create-purchase-invoice": create_purchase_invoice,
    "add-vendor-bill-intake": add_vendor_bill_intake,
    "capture-vendor-bill": capture_vendor_bill,
    "add-captured-vendor-bill": add_captured_vendor_bill,
    "update-purchase-invoice": update_purchase_invoice,
    "get-purchase-invoice": get_purchase_invoice,
    "list-purchase-invoices": list_purchase_invoices,
    "submit-purchase-invoice": submit_purchase_invoice,
    "cancel-purchase-invoice": cancel_purchase_invoice,
    "create-debit-note": create_debit_note,
    # RETIRED — routable on purpose; answers with a steer and writes nothing.
    "update-invoice-outstanding": update_invoice_outstanding,
    "add-landed-cost-voucher": add_landed_cost_voucher,
    "list-landed-cost-vouchers": list_landed_cost_vouchers,
    "get-landed-cost-voucher": get_landed_cost_voucher,
    "list-landed-cost-voucher-anomalies": list_landed_cost_voucher_anomalies,
    "cancel-landed-cost-voucher": cancel_landed_cost_voucher,
    "import-suppliers": import_suppliers,
    "update-receipt-tolerance": update_receipt_tolerance,
    "update-three-way-match-policy": update_three_way_match_policy,
    "add-blanket-po": add_blanket_po,
    "submit-blanket-po": submit_blanket_po,
    "get-blanket-po": get_blanket_po,
    "list-blanket-pos": list_blanket_pos,
    "create-po-from-blanket": create_po_from_blanket,
    "create-po-from-so": create_po_from_so,
    "add-recurring-bill-template": add_recurring_bill_template,
    "list-recurring-bill-templates": list_recurring_bill_templates,
    "generate-recurring-bills": generate_recurring_bills,
    "update-recurring-bill-template": update_recurring_bill_template,

    # --- Sprint 7: Multi-UOM ---
    "set-item-purchase-uom": set_item_purchase_uom,

    "status": status_action,
}


def main():
    parser = SafeArgumentParser(description="ERPClaw Buying Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Supplier fields
    parser.add_argument("--supplier-id")
    parser.add_argument("--name")
    parser.add_argument("--supplier-group")
    parser.add_argument("--supplier-type")
    parser.add_argument("--payment-terms-id")
    parser.add_argument("--tax-id")
    parser.add_argument("--is-1099-vendor")
    parser.add_argument("--primary-address")
    parser.add_argument("--email")
    parser.add_argument("--phone")
    parser.add_argument("--company-id")
    parser.add_argument("--company", dest="company_name", default=None)
    parser.add_argument("--csv-path")

    # Material request
    parser.add_argument("--material-request-id")
    parser.add_argument("--request-type")
    parser.add_argument("--mr-status", dest="mr_status")

    # RFQ
    parser.add_argument("--rfq-id")
    parser.add_argument("--communication-kind")
    parser.add_argument("--suppliers")  # JSON
    parser.add_argument("--rfq-status", dest="rfq_status")

    # Purchase order
    parser.add_argument("--purchase-order-id")
    parser.add_argument("--tax-template-id")
    parser.add_argument("--posting-date")
    parser.add_argument("--po-status", dest="po_status")

    # Purchase receipt
    parser.add_argument("--purchase-receipt-id")
    parser.add_argument("--purchase-receipt-ids")  # JSON for landed cost
    parser.add_argument("--pr-status", dest="pr_status")
    # Wave 2 S5 subcontracting receipt delegation (§Decision 6 single-post):
    # when set, create-purchase-receipt defers the FG receipt to manufacturing's
    # receive-subcontracted-items and posts nothing itself.
    parser.add_argument("--subcontracting-order-id", dest="subcontracting_order_id")
    parser.add_argument("--received-qty", dest="received_qty")
    parser.add_argument("--subcontract-charge-rate", dest="subcontract_charge_rate")

    # Purchase invoice
    parser.add_argument("--bill-json")
    parser.add_argument("--capture-file")
    parser.add_argument("--capture-sha256")
    parser.add_argument("--worksheet-json")
    parser.add_argument("--worksheet-id")
    parser.add_argument("--purchase-invoice-id")
    parser.add_argument("--due-date")
    parser.add_argument("--pi-status", dest="pi_status")
    # S3 CWIP hook (AVA-43): capitalise this bill to a construction-in-progress asset
    parser.add_argument("--cwip-asset-id")

    # Debit note / close fields
    parser.add_argument("--against-invoice-id")
    parser.add_argument("--reason")
    parser.add_argument("--closed-by")

    # Cross-skill
    parser.add_argument("--amount")

    # Landed cost
    parser.add_argument("--charges")  # JSON
    parser.add_argument("--landed-cost-voucher-id")
    parser.add_argument("--lcv-status", dest="lcv_status")

    # GRN tolerance & 3-way match policy
    parser.add_argument("--tolerance-pct")
    parser.add_argument("--policy")

    # Blanket order fields
    parser.add_argument("--blanket-order-id")
    parser.add_argument("--valid-from")
    parser.add_argument("--valid-to")
    parser.add_argument("--blanket-status", dest="blanket_status")

    # Back-to-back
    parser.add_argument("--sales-order-id")

    # Recurring bill template fields
    parser.add_argument("--template-id")
    parser.add_argument("--frequency")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--as-of-date")
    parser.add_argument("--auto-submit", dest="auto_submit", action="store_true", default=False)
    parser.add_argument("--template-status", dest="template_status")

    # Multi-UOM (Feature #19)
    parser.add_argument("--item-id")
    parser.add_argument("--purchase-uom")
    parser.add_argument("--conversion-factor")

    parser.add_argument("--dimensions", default=None)
    parser.add_argument("--dimension-key", dest="dimension_key",
                        action="append", default=None)
    parser.add_argument("--dimension-value", dest="dimension_value",
                        action="append", default=None)

    # Common
    parser.add_argument("--items")  # JSON
    parser.add_argument("--search")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")
    parser.add_argument("--custom-fields", default=None,
                        help='User-defined fields as a JSON object, e.g. \'{"rating": "A"}\'')

    raw = sys.argv[1:]
    try:
        parse_argv, _auth_id = authority_gate.split_authorization_id(raw)
    except ValueError:
        err(INPUT_INVALID)
    args, unknown = parser.parse_known_args(parse_argv)
    check_unknown_args(parser, unknown)
    check_input_lengths(args)

    db_path = getattr(args, "db_path", None)   # None unless --db-path was given
    conn = get_connection(db_path)

    # Dependency check
    _dep = check_required_tables(conn, REQUIRED_TABLES)
    if _dep:
        _dep["suggestion"] = "clawhub install " + " ".join(_dep.get("missing_skills", []))
        print(json.dumps(_dep, indent=2))
        conn.close()
        sys.exit(1)

    def _handler(handle):
        _resolve_company_flag(handle, args)
        return ACTIONS[args.action](handle, args)

    try:
        authority_gate.run(conn, args.action, raw, _handler, option_strings=[s for a in parser._actions for s in a.option_strings], repeatable_options=[s for a in parser._actions if isinstance(a, argparse._AppendAction) for s in a.option_strings])
    except authority_gate.AuthorityRefusal as refusal:
        conn.rollback()
        err(refusal.args[0], suggestion=authority_gate.SUGGESTIONS.get(refusal.args[0]))
    except Exception as e:
        if isinstance(e, ValueError) and e.args == (INPUT_INVALID,):
            conn.rollback()
            err(INPUT_INVALID)
        conn.rollback()
        sys.stderr.write(f"[erpclaw-buying] {e}\n")
        err(unexpected_error_message(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
