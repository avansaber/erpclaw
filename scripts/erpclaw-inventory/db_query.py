#!/usr/bin/env python3
"""ERPClaw Inventory Skill — db_query.py

Items, warehouses, stock entries, stock ledger, batches, serial numbers,
pricing, and stock reconciliation. Draft->Submit->Cancel lifecycle for
stock entries and reconciliation. Submit posts SLE + GL via shared lib.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import itertools
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# Add shared lib to path
try:
    sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH
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
    from erpclaw_lib.gl_posting import insert_gl_entries, reverse_gl_entries
    from erpclaw_lib.voucher_types import canonical_voucher_type
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.custom_fields import store_from_arg, merge_into_response
    from erpclaw_lib.dependencies import check_required_tables
    from erpclaw_lib.query_helpers import resolve_company_id
    from erpclaw_lib.query import Q, P, Table, Field, fn, DecimalSum, DecimalAbs, dynamic_update, line_order, latest_insert_order, now
    from erpclaw_lib.vendor.pypika import Order
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args
    from erpclaw_lib.vendor.pypika.terms import LiteralValue, ValueWrapper
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw first: clawhub install erpclaw", "suggestion": "clawhub install erpclaw"}))
    sys.exit(1)

# Convenience alias for CAST(CURRENT_TIMESTAMP AS TEXT) SQLite expression
_NOW = now()

REQUIRED_TABLES = ["company"]

VALID_ITEM_TYPES = ("stock", "non_stock", "service")
VALID_VALUATION_METHODS = ("moving_average", "fifo")
VALID_WAREHOUSE_TYPES = ("stores", "production", "transit", "rejected")
VALID_SERIAL_STATUSES = ("active", "delivered", "returned", "scrapped")

# User-friendly entry type -> DB value
ENTRY_TYPE_MAP = {
    "receive": "material_receipt",
    "issue": "material_issue",
    "transfer": "material_transfer",
    "manufacture": "manufacture",
    # User-friendly aliases for the S6 typed-dispatch paths
    "repack": "repack",
    "subcontract": "send_to_subcontractor",
    "consume": "material_consumption",
    # Also accept DB values directly
    "material_receipt": "material_receipt",
    "material_issue": "material_issue",
    "material_transfer": "material_transfer",
    "send_to_subcontractor": "send_to_subcontractor",
    "material_consumption": "material_consumption",
}

# Repack cost-balance tolerance: total input value must equal total output value
# within this Decimal band ($0.01 rounding allowance). A repack neither creates
# nor destroys inventory value — it only re-packages it, so cost-in must equal
# cost-out. The constant is a single point of truth (S6 §Validation rules).
REPACK_COST_TOLERANCE = Decimal("0.01")

# Warehouse types a subcontractor sub-store may legitimately be (S6 §Validation).
SUBCONTRACTOR_WAREHOUSE_TYPES = ("transit", "production")

# Work-order statuses that accept material consumption (an "active" work order).
# draft/completed/stopped/cancelled cannot consume; not_started/in_process can.
ACTIVE_WORK_ORDER_STATUSES = ("not_started", "in_process")



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


def _get_fiscal_year(conn, posting_date: str) -> str | None:
    """Return the fiscal year name for a posting date, or None."""
    fy = conn.execute(
        "SELECT name FROM fiscal_year WHERE start_date <= ? AND end_date >= ? AND is_closed = 0",
        (posting_date, posting_date),
    ).fetchone()
    return fy["name"] if fy else None


def _get_cost_center(conn, company_id: str) -> str | None:
    """Return the first non-group cost center for a company, or None."""
    t = Table("cost_center")
    q = (Q.from_(t).select(t.id)
         .where(t.company_id == P())
         .where(t.is_group == 0)
         .limit(1))
    cc = conn.execute(q.get_sql(), (company_id,)).fetchone()
    return cc["id"] if cc else None


# ---------------------------------------------------------------------------
# 1. add-item
# ---------------------------------------------------------------------------

def add_item(conn, args):
    """Create a new item."""
    if not args.item_code:
        err("--item-code is required")
    if not args.item_name:
        err("--item-name is required")

    item_type = args.item_type or "stock"
    if item_type not in VALID_ITEM_TYPES:
        err(f"--item-type must be one of: {', '.join(VALID_ITEM_TYPES)}")

    valuation_method = args.valuation_method or "moving_average"
    if valuation_method not in VALID_VALUATION_METHODS:
        err(f"--valuation-method must be one of: {', '.join(VALID_VALUATION_METHODS)}")

    # Validate item group if provided (accept id or name)
    if args.item_group:
        ig_t = Table("item_group")
        ig_q = (Q.from_(ig_t).select(ig_t.id)
                .where((ig_t.id == P()) | (ig_t.name == P())))
        ig = conn.execute(ig_q.get_sql(), (args.item_group, args.item_group)).fetchone()
        if not ig:
            err(f"Item group {args.item_group} not found")
        args.item_group = ig[0]  # normalize to id

    is_stock_item = 1 if item_type == "stock" else 0
    has_batch = int(args.has_batch) if args.has_batch else 0
    has_serial = int(args.has_serial) if args.has_serial else 0
    standard_rate = str(round_currency(to_decimal(args.standard_rate or "0")))

    item_id = str(uuid.uuid4())
    t = Table("item")
    q = Q.into(t).columns(
        "id", "item_code", "item_name", "item_group_id", "item_type", "stock_uom",
        "valuation_method", "is_stock_item", "has_batch", "has_serial",
        "standard_rate", "status",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), "active")
    try:
        conn.execute(
            q.get_sql(),
            (item_id, args.item_code, args.item_name, args.item_group,
             item_type, args.stock_uom or "Nos",
             valuation_method, is_stock_item, has_batch, has_serial,
             standard_rate),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err("Item creation failed — check for duplicates or invalid data")

    cf_errors = store_from_arg(conn, "item", item_id, getattr(args, "custom_fields", None))
    if cf_errors:
        conn.rollback()
        err("Custom field error: " + "; ".join(cf_errors))

    audit(conn, "erpclaw-inventory", "add-item", "item", item_id,
           new_values={"item_code": args.item_code, "item_name": args.item_name})
    conn.commit()
    resp = {"item_id": item_id, "item_code": args.item_code, "item_name": args.item_name}
    ok(merge_into_response(conn, "item", item_id, resp))


# ---------------------------------------------------------------------------
# 2. update-item
# ---------------------------------------------------------------------------

def update_item(conn, args):
    """Update an existing item."""
    if not args.item_id:
        err("--item-id is required")

    t = Table("item")
    q = Q.from_(t).select(t.star).where(t.id == P())
    item = conn.execute(q.get_sql(), (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found",
             suggestion="Use 'list items' to see available items.")

    if item["status"] == "disabled" and args.item_status != "active":
        err("Cannot update a disabled item (set --status active first)")

    data, updated_fields = {}, []

    if args.item_name is not None:
        data["item_name"] = args.item_name
        updated_fields.append("item_name")
    if args.reorder_level is not None:
        data["reorder_level"] = args.reorder_level
        updated_fields.append("reorder_level")
    if args.reorder_qty is not None:
        data["reorder_qty"] = args.reorder_qty
        updated_fields.append("reorder_qty")
    if args.standard_rate is not None:
        data["standard_rate"] = str(round_currency(to_decimal(args.standard_rate)))
        updated_fields.append("standard_rate")
    if args.item_status is not None:
        if args.item_status not in ("active", "disabled"):
            err("--status must be 'active' or 'disabled'")
        data["status"] = args.item_status
        updated_fields.append("status")

    if not updated_fields:
        err("No fields to update")

    data["updated_at"] = now()
    sql, params = dynamic_update("item", data, where={"id": args.item_id})
    conn.execute(sql, params)

    audit(conn, "erpclaw-inventory", "update-item", "item", args.item_id,
           new_values={"updated_fields": updated_fields})
    conn.commit()
    ok({"item_id": args.item_id, "updated_fields": updated_fields})


# ---------------------------------------------------------------------------
# 3. get-item
# ---------------------------------------------------------------------------

def get_item(conn, args):
    """Get item with stock summary across all warehouses."""
    if not args.item_id:
        err("--item-id is required")

    t = Table("item")
    q = Q.from_(t).select(t.star).where(t.id == P())
    item = conn.execute(q.get_sql(), (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")

    data = row_to_dict(item)

    # Stock balances per warehouse
    sle = Table("stock_ledger_entry")
    wh_q = (Q.from_(sle)
            .select(sle.warehouse_id).distinct()
            .where(sle.item_id == P())
            .where(sle.is_cancelled == 0))
    warehouses = conn.execute(wh_q.get_sql(), (args.item_id,)).fetchall()

    stock_balances = []
    total_qty = Decimal("0")
    total_value = Decimal("0")
    for wh_row in warehouses:
        wh_id = wh_row["warehouse_id"]
        balance = get_stock_balance(conn, args.item_id, wh_id)
        qty = to_decimal(balance["qty"])
        val = to_decimal(balance["stock_value"])
        if qty != 0 or val != 0:
            wh_t = Table("warehouse")
            wh_q2 = Q.from_(wh_t).select(wh_t.name).where(wh_t.id == P())
            wh = conn.execute(wh_q2.get_sql(), (wh_id,)).fetchone()
            stock_balances.append({
                "warehouse_id": wh_id,
                "warehouse_name": wh["name"] if wh else wh_id,
                "qty": balance["qty"],
                "valuation_rate": balance["valuation_rate"],
                "stock_value": balance["stock_value"],
            })
            total_qty += qty
            total_value += val

    data["stock_balances"] = stock_balances
    data["total_qty"] = str(round_currency(total_qty))
    data["total_stock_value"] = str(round_currency(total_value))
    ok(merge_into_response(conn, "item", item["id"], data))


# ---------------------------------------------------------------------------
# 4. list-items
# ---------------------------------------------------------------------------

def list_items(conn, args):
    """Query items with filtering."""
    i = Table("item").as_("i")
    ig = Table("item_group").as_("ig")

    # Warehouse filter: items that have stock in a specific warehouse
    warehouse_id = getattr(args, "warehouse_id", None)

    company_id = getattr(args, "company_id", None)

    # Build count query
    count_q = Q.from_(i).select(fn.Count("*"))
    if company_id:
        count_q = count_q.join(ig).on(ig.id == i.item_group_id).where(ig.company_id == P())
    if warehouse_id:
        sle = Table("stock_ledger_entry")
        sub = (Q.from_(sle).select(sle.item_id).distinct()
               .where(sle.warehouse_id == P()).where(sle.is_cancelled == 0))
        count_q = count_q.where(i.id.isin(sub))
    if args.item_group:
        count_q = count_q.where(i.item_group_id == P())
    if args.item_type:
        count_q = count_q.where(i.item_type == P())
    if args.search:
        count_q = count_q.where(
            (i.item_name.like(P())) | (i.item_code.like(P()))
        )

    count_params = []
    if company_id:
        count_params.append(company_id)
    if warehouse_id:
        count_params.append(warehouse_id)
    if args.item_group:
        count_params.append(args.item_group)
    if args.item_type:
        count_params.append(args.item_type)
    if args.search:
        count_params.extend([f"%{args.search}%", f"%{args.search}%"])

    count_row = conn.execute(count_q.get_sql(), count_params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0

    rows_q = (Q.from_(i)
              .left_join(ig).on(ig.id == i.item_group_id)
              .select(i.id, i.item_code, i.item_name, i.item_group_id,
                      ig.name.as_("item_group_name"),
                      i.item_type, i.stock_uom, i.standard_rate, i.status,
                      i.has_batch, i.has_serial)
              .orderby(i.item_name)
              .limit(P()).offset(P()))
    if company_id:
        rows_q = rows_q.where(ig.company_id == P())
    if warehouse_id:
        sle = Table("stock_ledger_entry")
        sub = (Q.from_(sle).select(sle.item_id).distinct()
               .where(sle.warehouse_id == P()).where(sle.is_cancelled == 0))
        rows_q = rows_q.where(i.id.isin(sub))
    if args.item_group:
        rows_q = rows_q.where(i.item_group_id == P())
    if args.item_type:
        rows_q = rows_q.where(i.item_type == P())
    if args.search:
        rows_q = rows_q.where(
            (i.item_name.like(P())) | (i.item_code.like(P()))
        )

    row_params = []
    if company_id:
        row_params.append(company_id)
    if warehouse_id:
        row_params.append(warehouse_id)
    if args.item_group:
        row_params.append(args.item_group)
    if args.item_type:
        row_params.append(args.item_type)
    if args.search:
        row_params.extend([f"%{args.search}%", f"%{args.search}%"])
    row_params.extend([limit, offset])

    rows = conn.execute(rows_q.get_sql(), row_params).fetchall()

    ok({"items": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# resolve-item — deterministic cascade resolver (FINDING-008)
# ---------------------------------------------------------------------------

def _singularize(phrase):
    """Conservative, stdlib-only English singularizer (no nltk/inflect).

    Operates on the last whitespace token of the phrase only (so "Brake Pad
    Sets" -> "Brake Pad Set", not mangled mid-phrase). Intentionally lossy:
    over-singularizing causes false matches, so the rules stay conservative
    and tiers 3-4 (substring / token-AND) are the safety net for the rest.
    """
    parts = phrase.split()
    if not parts:
        return phrase
    w = parts[-1]
    if len(w) > 3 and w.endswith("ies"):
        w = w[:-3] + "y"          # categories -> category
    elif len(w) > 2 and w.endswith("es") and w[-3:-2] in ("s", "x", "z", "h"):
        w = w[:-2]                # boxes -> box, batches -> batch
    elif len(w) > 1 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]                # sets -> set; "glass"/"chassis" kept via ss guard
    parts[-1] = w
    return " ".join(parts)


_RESOLVE_LIMIT = 10


def _resolve_select(i):
    """Common SELECT/order/limit skeleton for a resolver tier (shortest name first)."""
    return (Q.from_(i)
            .select(i.id.as_("item_id"), i.item_code, i.item_name,
                    i.item_type, i.stock_uom, i.standard_rate, i.status)
            .orderby(fn.Length(i.item_name)).orderby(i.item_name)
            .limit(P()))


def resolve_item(conn, args):
    """Resolve a loose/plural user phrase to ranked item candidates.

    READ-ONLY. Deterministic 4-tier cascade; stops at the first tier that
    returns >=1 row. All comparison is dialect-neutral via fn.Lower(field)
    against a Python-.lower()-ed term (never .ilike()). See FINDING-008.
    """
    name = getattr(args, "name", None)
    if not name or not name.strip():
        err("--name is required")

    term = name.strip()
    low = term.lower()

    i = Table("item").as_("i")

    def run(query, params):
        rows = conn.execute(query.get_sql(), params).fetchall()
        return [row_to_dict(r) for r in rows]

    candidates = []
    match_type = None

    # Tier 1 — exact (case-insensitive) on item_name OR item_code.
    q1 = _resolve_select(i).where(
        (fn.Lower(i.item_name) == P()) | (fn.Lower(i.item_code) == P()))
    candidates = run(q1, [low, low, _RESOLVE_LIMIT])
    if candidates:
        match_type = "exact"

    # Tier 2 — singularized exact, then singularized LIKE (only if plural).
    if not candidates:
        sing = _singularize(low)
        if sing != low:
            q2a = _resolve_select(i).where(
                (fn.Lower(i.item_name) == P()) | (fn.Lower(i.item_code) == P()))
            candidates = run(q2a, [sing, sing, _RESOLVE_LIMIT])
            if not candidates:
                pat = f"%{sing}%"
                q2b = _resolve_select(i).where(
                    fn.Lower(i.item_name).like(P()) | fn.Lower(i.item_code).like(P()))
                candidates = run(q2b, [pat, pat, _RESOLVE_LIMIT])
            if candidates:
                match_type = "singular"

    # Tier 3 — substring LIKE on the raw term (item_name OR item_code).
    if not candidates:
        pat = f"%{low}%"
        q3 = _resolve_select(i).where(
            fn.Lower(i.item_name).like(P()) | fn.Lower(i.item_code).like(P()))
        candidates = run(q3, [pat, pat, _RESOLVE_LIMIT])
        if candidates:
            match_type = "substring"

    # Tier 4 — token-AND (every token contained), only if 1-3 empty.
    if not candidates:
        tokens = [_singularize(tok) for tok in low.split() if len(tok) >= 2]
        if tokens:
            q4 = _resolve_select(i)
            params = []
            for tok in tokens:
                q4 = q4.where(fn.Lower(i.item_name).like(P()))
                params.append(f"%{tok}%")
            params.append(_RESOLVE_LIMIT)
            candidates = run(q4, params)
            if candidates:
                match_type = "tokens"

    matched = len(candidates) >= 1
    ok({
        "query": term,
        "matched": matched,
        "single_match": len(candidates) == 1,
        "multiple_matches": len(candidates) > 1,
        "match_type": match_type if matched else None,
        "candidates": candidates,
    })


# ---------------------------------------------------------------------------
# 5. add-item-group
# ---------------------------------------------------------------------------

def add_item_group(conn, args):
    """Create an item group."""
    if not args.name:
        err("--name is required")

    company_id = getattr(args, "company_id", None)

    if args.parent_id:
        ig_t = Table("item_group")
        parent_q = Q.from_(ig_t).select(ig_t.id).where(ig_t.id == P())
        parent = conn.execute(parent_q.get_sql(), (args.parent_id,)).fetchone()
        if not parent:
            err(f"Parent item group {args.parent_id} not found")

    ig_id = str(uuid.uuid4())
    t = Table("item_group")
    q = Q.into(t).columns("id", "name", "company_id", "parent_id").insert(P(), P(), P(), P())
    try:
        conn.execute(q.get_sql(), (ig_id, args.name, company_id, args.parent_id))
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err(f"Item group '{args.name}' already exists"
            f"{' for this company' if company_id else ''}"
            f". Choose a different name or update the existing group.")

    audit(conn, "erpclaw-inventory", "add-item-group", "item_group", ig_id,
           new_values={"name": args.name})
    conn.commit()
    ok({"item_group_id": ig_id, "name": args.name})


# ---------------------------------------------------------------------------
# 6. list-item-groups
# ---------------------------------------------------------------------------

def list_item_groups(conn, args):
    """List item groups."""
    t = Table("item_group")

    company_id = getattr(args, "company_id", None)

    count_q = Q.from_(t).select(fn.Count("*"))
    if company_id:
        count_q = count_q.where(t.company_id == P())
    if args.parent_id:
        count_q = count_q.where(t.parent_id == P())

    count_params = []
    if company_id:
        count_params.append(company_id)
    if args.parent_id:
        count_params.append(args.parent_id)

    count_row = conn.execute(count_q.get_sql(), count_params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0

    rows_q = (Q.from_(t).select(t.star)
              .orderby(t.name)
              .limit(P()).offset(P()))
    if company_id:
        rows_q = rows_q.where(t.company_id == P())
    if args.parent_id:
        rows_q = rows_q.where(t.parent_id == P())

    row_params = []
    if company_id:
        row_params.append(company_id)
    if args.parent_id:
        row_params.append(args.parent_id)
    row_params.extend([limit, offset])

    rows = conn.execute(rows_q.get_sql(), row_params).fetchall()

    ok({"item_groups": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 7. add-warehouse
# ---------------------------------------------------------------------------

def add_warehouse(conn, args):
    """Create a warehouse."""
    if not args.name:
        err("--name is required")
    if not args.company_id:
        err("--company-id is required")

    co_t = Table("company")
    co_q = Q.from_(co_t).select(co_t.id).where(co_t.id == P())
    if not conn.execute(co_q.get_sql(), (args.company_id,)).fetchone():
        err(f"Company {args.company_id} not found")

    wh_type = args.warehouse_type or "stores"
    if wh_type not in VALID_WAREHOUSE_TYPES:
        err(f"--warehouse-type must be one of: {', '.join(VALID_WAREHOUSE_TYPES)}")

    if args.parent_id:
        wh_t = Table("warehouse")
        parent_q = Q.from_(wh_t).select(wh_t.id).where(wh_t.id == P())
        parent = conn.execute(parent_q.get_sql(), (args.parent_id,)).fetchone()
        if not parent:
            err(f"Parent warehouse {args.parent_id} not found")

    if args.account_id:
        acct_t = Table("account")
        acct_q = Q.from_(acct_t).select(acct_t.id).where(acct_t.id == P())
        acct = conn.execute(acct_q.get_sql(), (args.account_id,)).fetchone()
        if not acct:
            err(f"Account {args.account_id} not found")

    is_group = int(args.is_group) if args.is_group else 0
    wh_id = str(uuid.uuid4())
    t = Table("warehouse")
    q = Q.into(t).columns(
        "id", "name", "parent_id", "warehouse_type",
        "company_id", "account_id", "is_group",
    ).insert(P(), P(), P(), P(), P(), P(), P())
    conn.execute(
        q.get_sql(),
        (wh_id, args.name, args.parent_id, wh_type,
         args.company_id, args.account_id, is_group),
    )

    audit(conn, "erpclaw-inventory", "add-warehouse", "warehouse", wh_id,
           new_values={"name": args.name, "type": wh_type})
    conn.commit()
    ok({"warehouse_id": wh_id, "name": args.name})


# ---------------------------------------------------------------------------
# 8. update-warehouse
# ---------------------------------------------------------------------------

def update_warehouse(conn, args):
    """Update a warehouse."""
    if not args.warehouse_id:
        err("--warehouse-id is required")

    wh_t = Table("warehouse")
    wh_q = (Q.from_(wh_t).select(wh_t.star)
            .where((wh_t.id == P()) | (wh_t.name == P())))
    wh = conn.execute(wh_q.get_sql(), (args.warehouse_id, args.warehouse_id)).fetchone()
    if not wh:
        err(f"Warehouse {args.warehouse_id} not found")
    args.warehouse_id = wh["id"]  # normalize to id

    data, updated_fields = {}, []

    if args.name is not None:
        data["name"] = args.name
        updated_fields.append("name")
    if args.account_id is not None:
        acct_t = Table("account")
        acct_q = Q.from_(acct_t).select(acct_t.id).where(acct_t.id == P())
        acct = conn.execute(acct_q.get_sql(), (args.account_id,)).fetchone()
        if not acct:
            err(f"Account {args.account_id} not found")
        data["account_id"] = args.account_id
        updated_fields.append("account_id")

    if not updated_fields:
        err("No fields to update")

    data["updated_at"] = now()
    sql, params = dynamic_update("warehouse", data, where={"id": args.warehouse_id})
    conn.execute(sql, params)

    audit(conn, "erpclaw-inventory", "update-warehouse", "warehouse", args.warehouse_id,
           new_values={"updated_fields": updated_fields})
    conn.commit()
    ok({"warehouse_id": args.warehouse_id, "updated_fields": updated_fields})


# ---------------------------------------------------------------------------
# 9. list-warehouses
# ---------------------------------------------------------------------------

def list_warehouses(conn, args):
    """List warehouses for a company."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    w = Table("warehouse").as_("w")

    count_q = Q.from_(w).select(fn.Count("*")).where(w.company_id == P())
    if args.parent_id:
        count_q = count_q.where(w.parent_id == P())
    if args.warehouse_type:
        count_q = count_q.where(w.warehouse_type == P())

    count_params = [company_id]
    if args.parent_id:
        count_params.append(args.parent_id)
    if args.warehouse_type:
        count_params.append(args.warehouse_type)

    count_row = conn.execute(count_q.get_sql(), count_params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0

    rows_q = (Q.from_(w).select(w.star)
              .where(w.company_id == P())
              .orderby(w.name)
              .limit(P()).offset(P()))
    if args.parent_id:
        rows_q = rows_q.where(w.parent_id == P())
    if args.warehouse_type:
        rows_q = rows_q.where(w.warehouse_type == P())

    row_params = [company_id]
    if args.parent_id:
        row_params.append(args.parent_id)
    if args.warehouse_type:
        row_params.append(args.warehouse_type)
    row_params.extend([limit, offset])

    rows = conn.execute(rows_q.get_sql(), row_params).fetchall()

    ok({"warehouses": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 10. add-stock-entry
# ---------------------------------------------------------------------------

def _guard_open_order_line(conn, entry_type, company_id, item_id):
    """Product-layer open-order guard (F8, stabilization WS1 D7).

    A standalone material_receipt for an item with an open purchase-order line
    would leave the PO un-received, so the Buying flow receives the goods AGAIN
    later and double-COUNTS stock. Symmetrically, a standalone material_issue
    for an item with an open sales-order line double-SUBTRACTS: the ad-hoc
    issue takes the stock out now and the delivery-note flow subtracts it
    again at delivery, understating inventory.

    Hard-block-with-guidance in the _enforce_credit_policy shape: refuse via
    err() BEFORE any write, naming the open order and pointing at the
    order-backed flow. READ-only on buying/selling-owned tables (any module
    may READ any table). Same-company scoping; an order whose line for this
    item is already fully received/delivered does not block, and neither do
    draft/closed/cancelled orders ("open" matches the create-purchase-receipt
    / create-delivery-note gates: confirmed or partially_received/delivered).
    """
    if entry_type == "material_receipt":
        # raw SQL — JOIN + CAST comparison on TEXT qty columns
        row = conn.execute(
            """SELECT po.id, po.naming_series
               FROM purchase_order po
               JOIN purchase_order_item poi ON poi.purchase_order_id = po.id
               WHERE po.company_id = ?
                 AND po.status IN ('confirmed', 'partially_received')
                 AND poi.item_id = ?
                 AND CAST(poi.quantity AS NUMERIC) > CAST(poi.received_qty AS NUMERIC)
               LIMIT 1""",
            (company_id, item_id),
        ).fetchone()
        if row:
            ref = row["naming_series"] or row["id"]
            err(f"Cannot receive item {item_id} as a standalone stock entry: "
                f"open purchase order {ref} still has this item waiting to be "
                f"received. Receive it against the order instead "
                f"(create-purchase-receipt --purchase-order-id {row['id']}, "
                f"then submit-purchase-receipt) so the order is marked "
                f"received and the stock is not counted twice.")
    elif entry_type == "material_issue":
        # raw SQL — JOIN + CAST comparison on TEXT qty columns
        row = conn.execute(
            """SELECT so.id, so.naming_series
               FROM sales_order so
               JOIN sales_order_item soi ON soi.sales_order_id = so.id
               WHERE so.company_id = ?
                 AND so.status IN ('confirmed', 'partially_delivered')
                 AND soi.item_id = ?
                 AND CAST(soi.quantity AS NUMERIC) > CAST(soi.delivered_qty AS NUMERIC)
               LIMIT 1""",
            (company_id, item_id),
        ).fetchone()
        if row:
            ref = row["naming_series"] or row["id"]
            err(f"Cannot issue item {item_id} as a standalone stock entry: "
                f"open sales order {ref} still has this item waiting to be "
                f"delivered. Deliver it against the order instead "
                f"(create-delivery-note --sales-order-id {row['id']}, "
                f"then submit-delivery-note) so the order is marked "
                f"delivered and the stock is not subtracted twice.")


def add_stock_entry(conn, args):
    """Create a stock entry in draft."""
    if not args.entry_type:
        err("--entry-type is required "
            "(receive|issue|transfer|manufacture|repack|subcontract|consume)")
    entry_type = ENTRY_TYPE_MAP.get(args.entry_type)
    if not entry_type:
        err(f"Invalid --entry-type '{args.entry_type}'. "
             f"Valid: receive, issue, transfer, manufacture, "
             f"repack, subcontract, consume")
    if not args.company_id:
        err("--company-id is required")
    if not args.posting_date:
        err("--posting-date is required")
    if not args.items:
        err("--items is required (JSON array)")

    co_t = Table("company")
    co_q = Q.from_(co_t).select(co_t.id).where(co_t.id == P())
    if not conn.execute(co_q.get_sql(), (args.company_id,)).fetchone():
        err(f"Company {args.company_id} not found")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    # --- S6 entry-level validation for the typed-dispatch paths ---------------
    # These checks are entry-wide (one parent reference), so run them once before
    # the per-item loop rather than per line.
    purpose_ref_type = None
    purpose_ref_id = None

    if entry_type == "send_to_subcontractor":
        # Materials move OUT to a supplier sub-store; that warehouse must be a
        # transit or production warehouse (a real subcontractor staging store),
        # never a normal stores/rejected warehouse.
        sub_wh_id = args.supplier_warehouse_id
        if not sub_wh_id:
            err("--supplier-warehouse-id is required for a subcontract transfer")
        wh_t = Table("warehouse")
        wh_q = (Q.from_(wh_t).select(wh_t.id, wh_t.warehouse_type)
                .where(wh_t.id == P()))
        wh_row = conn.execute(wh_q.get_sql(), (sub_wh_id,)).fetchone()
        if not wh_row:
            err(f"Subcontractor warehouse {sub_wh_id} not found")
        if wh_row["warehouse_type"] not in SUBCONTRACTOR_WAREHOUSE_TYPES:
            err(f"Subcontractor warehouse {sub_wh_id} has warehouse_type "
                f"'{wh_row['warehouse_type']}'; must be one of "
                f"{'/'.join(SUBCONTRACTOR_WAREHOUSE_TYPES)}")
        purpose_ref_type = "subcontracting_warehouse"
        purpose_ref_id = sub_wh_id

    elif entry_type == "material_consumption":
        # Raw materials are issued against an active work order.
        wo_id = args.work_order_id
        if not wo_id:
            err("--work-order-id is required for material consumption")
        wo_t = Table("work_order")
        wo_q = (Q.from_(wo_t).select(wo_t.id, wo_t.status)
                .where(wo_t.id == P()))
        wo_row = conn.execute(wo_q.get_sql(), (wo_id,)).fetchone()
        if not wo_row:
            err(f"Work order {wo_id} not found")
        if wo_row["status"] not in ACTIVE_WORK_ORDER_STATUSES:
            err(f"Work order {wo_id} is '{wo_row['status']}'; material can only "
                f"be consumed against an active work order "
                f"({'/'.join(ACTIVE_WORK_ORDER_STATUSES)})")
        purpose_ref_type = "work_order"
        purpose_ref_id = wo_id

    se_id = str(uuid.uuid4())
    naming = get_next_name(conn, "stock_entry", company_id=args.company_id)

    total_incoming = Decimal("0")
    total_outgoing = Decimal("0")

    # Validate and collect item rows before inserting
    item_rows_to_insert = []
    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Item {i}: item_id is required")

        # Validate item exists
        item_t = Table("item")
        item_q = (Q.from_(item_t)
                  .select(item_t.id, item_t.standard_rate)
                  .where(item_t.id == P()))
        item_row = conn.execute(item_q.get_sql(), (item_id,)).fetchone()
        if not item_row:
            err(f"Item {i}: item {item_id} not found")

        # Open-order guard (F8): refuse a standalone receipt/issue that would
        # bypass an open PO/SO line for this item. Blocks BEFORE any write.
        if entry_type in ("material_receipt", "material_issue"):
            _guard_open_order_line(conn, entry_type, args.company_id, item_id)

        qty = to_decimal(item.get("qty", "0"))
        if qty <= 0:
            err(f"Item {i}: qty must be > 0")

        rate = to_decimal(item.get("rate", "0"))
        if rate <= 0:
            rate = to_decimal(item_row["standard_rate"])

        amount = round_currency(qty * rate)

        from_wh = item.get("from_warehouse_id")
        to_wh = item.get("to_warehouse_id")

        # A repack line is directional: an INPUT line is consumed (from_wh only),
        # an OUTPUT line is produced (to_wh only). Auto-filling BOTH from the
        # company default would erase that distinction, so repack lines opt out of
        # the dual fallback below.
        is_repack_line = (entry_type == "repack")

        # Fall back to company's default warehouse if item-level warehouse not specified
        if (not to_wh or not from_wh) and not is_repack_line:
            dw_t = Table("company")
            dw_q = Q.from_(dw_t).select(dw_t.default_warehouse_id).where(dw_t.id == P())
            dw_row = conn.execute(dw_q.get_sql(), (args.company_id,)).fetchone()
            default_wh = dw_row["default_warehouse_id"] if dw_row else None
            if not to_wh and default_wh:
                to_wh = default_wh
            if not from_wh and default_wh:
                from_wh = default_wh

        # Validate warehouse per entry type
        if entry_type == "material_receipt":
            if not to_wh:
                err(f"Item {i}: to_warehouse_id is required for receipt (set company default warehouse or provide per-item)")
            total_incoming += amount
        elif entry_type == "material_issue":
            if not from_wh:
                err(f"Item {i}: from_warehouse_id is required for issue")
            total_outgoing += amount
        elif entry_type == "material_transfer":
            if not from_wh:
                err(f"Item {i}: from_warehouse_id is required for transfer")
            if not to_wh:
                err(f"Item {i}: to_warehouse_id is required for transfer")
            total_incoming += amount
            total_outgoing += amount
        elif entry_type == "manufacture":
            # Manufacture: finished goods go to to_warehouse, raw materials come from from_warehouse
            if not from_wh and not to_wh:
                err(f"Item {i}: from_warehouse_id or to_warehouse_id is required for manufacture")
            if to_wh:
                total_incoming += amount
            if from_wh:
                total_outgoing += amount
        elif entry_type == "repack":
            # A repack line is either an INPUT (consumed, from_wh only) or an
            # OUTPUT (produced, to_wh only). Exactly one of from/to must be set.
            if bool(from_wh) == bool(to_wh):
                err(f"Item {i}: a repack line needs exactly one of "
                    f"from_warehouse_id (input) or to_warehouse_id (output)")
            if to_wh:
                total_incoming += amount
            else:
                total_outgoing += amount
        elif entry_type == "send_to_subcontractor":
            # Move stock OUT of from_wh to the (validated) subcontractor sub-store.
            # The destination is the entry-level supplier_warehouse_id, not a
            # per-line to_warehouse; force it so the SLE builder posts both legs.
            if not from_wh:
                err(f"Item {i}: from_warehouse_id is required for a subcontract transfer")
            to_wh = purpose_ref_id  # validated supplier sub-store
            total_incoming += amount
            total_outgoing += amount
        elif entry_type == "material_consumption":
            # Raw materials issued against the work order — consumed from from_wh.
            if not from_wh:
                err(f"Item {i}: from_warehouse_id is required for material consumption")
            total_outgoing += amount

        item_rows_to_insert.append((
            str(uuid.uuid4()), se_id, item_id, str(round_currency(qty)),
            from_wh, to_wh,
            str(round_currency(rate)), str(amount),
            item.get("batch_id"), item.get("serial_numbers"),
        ))

    value_diff = round_currency(total_incoming - total_outgoing)

    # A repack neither creates nor destroys inventory value: total input value
    # must equal total output value within the $0.01 rounding tolerance. This is
    # the cost-balance invariant (S6 §Validation rules). It also guarantees both
    # an input and an output line exist (a zero on either side breaks balance for
    # any non-trivial repack, and a fully-zero repack is meaningless).
    if entry_type == "repack":
        if total_incoming <= 0 or total_outgoing <= 0:
            err("A repack needs at least one input line (consumed) and one "
                "output line (produced)")
        if abs(total_incoming - total_outgoing) > REPACK_COST_TOLERANCE:
            err(f"Repack is not cost-balanced: input value "
                f"{round_currency(total_outgoing)} != output value "
                f"{round_currency(total_incoming)} "
                f"(tolerance ${REPACK_COST_TOLERANCE})")

    # Insert parent stock_entry first (FK target for stock_entry_item)
    se_t = Table("stock_entry")
    se_q = Q.into(se_t).columns(
        "id", "naming_series", "stock_entry_type", "posting_date",
        "total_incoming_value", "total_outgoing_value", "value_difference",
        "purpose_reference_type", "purpose_reference_id",
        "status", "company_id",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), "draft", P())
    conn.execute(
        se_q.get_sql(),
        (se_id, naming, entry_type, args.posting_date,
         str(round_currency(total_incoming)),
         str(round_currency(total_outgoing)),
         str(value_diff), purpose_ref_type, purpose_ref_id, args.company_id),
    )

    # Now insert child stock_entry_item rows
    sei_t = Table("stock_entry_item")
    sei_q = Q.into(sei_t).columns(
        "id", "stock_entry_id", "item_id", "quantity", "from_warehouse_id",
        "to_warehouse_id", "valuation_rate", "amount", "batch_id", "serial_numbers",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P())
    for row_params in item_rows_to_insert:
        conn.execute(sei_q.get_sql(), row_params)

    audit(conn, "erpclaw-inventory", "add-stock-entry", "stock_entry", se_id,
           new_values={"naming_series": naming, "type": entry_type,
                       "item_count": len(items)})
    conn.commit()
    ok({"stock_entry_id": se_id, "naming_series": naming,
         "total_incoming_value": str(round_currency(total_incoming)),
         "total_outgoing_value": str(round_currency(total_outgoing)),
         "value_difference": str(value_diff)})


def add_repack_stock_entry(conn, args):
    """Convenience wrapper: a one-input/one-output repack in a single warehouse.

    Builds the two-line --items payload (consume from_item, produce to_item, both
    in --warehouse) and delegates to add_stock_entry with --entry-type repack, so
    the cost-balance invariant + repack dispatch live in exactly one place.
    """
    if not args.warehouse:
        err("--warehouse is required for a repack")
    if not args.from_item_id or not args.to_item_id:
        err("--from-item-id and --to-item-id are required for a repack")
    if not args.from_qty or not args.to_qty:
        err("--from-qty and --to-qty are required for a repack")

    items = [
        {"item_id": args.from_item_id, "qty": args.from_qty,
         "from_warehouse_id": args.warehouse},
    ]
    out_line = {"item_id": args.to_item_id, "qty": args.to_qty,
                "to_warehouse_id": args.warehouse}
    # An explicit --standard-rate values the produced item; otherwise it falls
    # back to the item's standard_rate (same rule as add_stock_entry lines).
    if args.standard_rate:
        out_line["rate"] = args.standard_rate
    items.append(out_line)

    args.entry_type = "repack"
    args.items = json.dumps(items)
    add_stock_entry(conn, args)


def add_material_consumption(conn, args):
    """Convenience wrapper: issue one raw material against a work order.

    Builds the single-line --items payload (consume --item-id from --warehouse)
    and delegates to add_stock_entry with --entry-type material_consumption.
    """
    if not args.warehouse:
        err("--warehouse is required for material consumption")
    if not args.work_order_id:
        err("--work-order-id is required for material consumption")
    if not args.item_id:
        err("--item-id is required for material consumption")
    if not args.qty:
        err("--qty is required for material consumption")

    line = {"item_id": args.item_id, "qty": args.qty,
            "from_warehouse_id": args.warehouse}
    if args.rate:
        line["rate"] = args.rate

    args.entry_type = "material_consumption"
    args.items = json.dumps([line])
    add_stock_entry(conn, args)


# ---------------------------------------------------------------------------
# 11. get-stock-entry
# ---------------------------------------------------------------------------

def get_stock_entry(conn, args):
    """Get stock entry with items."""
    if not args.stock_entry_id:
        err("--stock-entry-id is required")

    se_t = Table("stock_entry")
    se_q = Q.from_(se_t).select(se_t.star).where(se_t.id == P())
    se = conn.execute(se_q.get_sql(), (args.stock_entry_id,)).fetchone()
    if not se:
        err(f"Stock entry {args.stock_entry_id} not found")

    data = row_to_dict(se)

    sei = Table("stock_entry_item").as_("sei")
    i = Table("item").as_("i")
    items_q = (Q.from_(sei)
               .left_join(i).on(i.id == sei.item_id)
               .select(sei.star, i.item_code, i.item_name)
               .where(sei.stock_entry_id == P())
               .orderby(line_order(sei)))
    items = conn.execute(items_q.get_sql(), (args.stock_entry_id,)).fetchall()
    data["items"] = [row_to_dict(r) for r in items]
    ok(data)


# ---------------------------------------------------------------------------
# 12. list-stock-entries
# ---------------------------------------------------------------------------

def list_stock_entries(conn, args):
    """List stock entries with filtering."""
    se = Table("stock_entry").as_("se")

    count_q = Q.from_(se).select(fn.Count("*"))
    if args.company_id:
        count_q = count_q.where(se.company_id == P())
    if args.entry_type:
        mapped = ENTRY_TYPE_MAP.get(args.entry_type, args.entry_type)
        count_q = count_q.where(se.stock_entry_type == P())
    if args.se_status:
        count_q = count_q.where(se.status == P())
    if args.from_date:
        count_q = count_q.where(se.posting_date >= P())
    if args.to_date:
        count_q = count_q.where(se.posting_date <= P())

    count_params = []
    if args.company_id:
        count_params.append(args.company_id)
    if args.entry_type:
        count_params.append(ENTRY_TYPE_MAP.get(args.entry_type, args.entry_type))
    if args.se_status:
        count_params.append(args.se_status)
    if args.from_date:
        count_params.append(args.from_date)
    if args.to_date:
        count_params.append(args.to_date)

    count_row = conn.execute(count_q.get_sql(), count_params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0

    rows_q = (Q.from_(se)
              .select(se.id, se.naming_series, se.stock_entry_type, se.posting_date,
                      se.total_incoming_value, se.total_outgoing_value,
                      se.value_difference, se.status, se.company_id)
              .orderby(se.posting_date, order=Order.desc)
              .orderby(se.created_at, order=Order.desc)
              .limit(P()).offset(P()))
    if args.company_id:
        rows_q = rows_q.where(se.company_id == P())
    if args.entry_type:
        rows_q = rows_q.where(se.stock_entry_type == P())
    if args.se_status:
        rows_q = rows_q.where(se.status == P())
    if args.from_date:
        rows_q = rows_q.where(se.posting_date >= P())
    if args.to_date:
        rows_q = rows_q.where(se.posting_date <= P())

    row_params = []
    if args.company_id:
        row_params.append(args.company_id)
    if args.entry_type:
        row_params.append(ENTRY_TYPE_MAP.get(args.entry_type, args.entry_type))
    if args.se_status:
        row_params.append(args.se_status)
    if args.from_date:
        row_params.append(args.from_date)
    if args.to_date:
        row_params.append(args.to_date)
    row_params.extend([limit, offset])

    rows = conn.execute(rows_q.get_sql(), row_params).fetchall()

    ok({"stock_entries": [row_to_dict(r) for r in rows],
         "total_count": total_count, "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 13. submit-stock-entry
# ---------------------------------------------------------------------------

def submit_stock_entry(conn, args):
    """Submit a draft stock entry: post SLE + GL entries."""
    if not args.stock_entry_id:
        err("--stock-entry-id is required")

    se_t = Table("stock_entry")
    se_q = Q.from_(se_t).select(se_t.star).where(se_t.id == P())
    se = conn.execute(se_q.get_sql(), (args.stock_entry_id,)).fetchone()
    if not se:
        err(f"Stock entry {args.stock_entry_id} not found")
    if se["status"] != "draft":
        err(f"Cannot submit: stock entry is '{se['status']}' (must be 'draft')")

    se_dict = row_to_dict(se)
    company_id = se_dict["company_id"]
    posting_date = se_dict["posting_date"]
    entry_type = se_dict["stock_entry_type"]

    # Fetch items
    sei_t = Table("stock_entry_item")
    sei_q = (Q.from_(sei_t).select(sei_t.star)
             .where(sei_t.stock_entry_id == P())
             .orderby(line_order()))
    items = conn.execute(sei_q.get_sql(), (args.stock_entry_id,)).fetchall()
    if not items:
        err("Stock entry has no items")

    # Open-order guard (F8): re-checked at submit because an order may have
    # been confirmed after the draft was created. Blocks BEFORE any SLE/GL
    # write (nothing below is written until insert_sle_entries).
    if entry_type in ("material_receipt", "material_issue"):
        for item_row in items:
            _guard_open_order_line(conn, entry_type, company_id,
                                   item_row["item_id"])

    # Find fiscal year for the posting date
    fiscal_year = _get_fiscal_year(conn, posting_date)

    # Find cost center for P&L accounts (COGS)
    cost_center_id = _get_cost_center(conn, company_id)

    # Build SLE entries from stock entry items
    sle_entries = []
    for item_row in items:
        item = row_to_dict(item_row)
        qty = to_decimal(item["quantity"])
        rate = to_decimal(item["valuation_rate"])
        from_wh = item.get("from_warehouse_id")
        to_wh = item.get("to_warehouse_id")

        if entry_type == "material_receipt":
            # Positive qty at to_warehouse
            sle_entries.append({
                "item_id": item["item_id"],
                "warehouse_id": to_wh,
                "actual_qty": str(round_currency(qty)),
                "incoming_rate": str(round_currency(rate)),
                "batch_id": item.get("batch_id"),
                "serial_number": item.get("serial_numbers"),
                "fiscal_year": fiscal_year,
                # FINDING-010 / ADR-0014: a standalone material_receipt is a true
                # external receipt — it must carry a stated rate (or the item's
                # standard_rate), never silently book inventory at $0. Internal
                # moves (transfer-in, manufacture FG leg) below do NOT opt in.
                "require_rate": True,
            })
        elif entry_type == "material_issue":
            # Negative qty at from_warehouse
            sle_entries.append({
                "item_id": item["item_id"],
                "warehouse_id": from_wh,
                "actual_qty": str(round_currency(-qty)),
                "incoming_rate": "0",
                "batch_id": item.get("batch_id"),
                "serial_number": item.get("serial_numbers"),
                "fiscal_year": fiscal_year,
            })
        elif entry_type == "material_transfer":
            # Negative at from_warehouse, positive at to_warehouse
            sle_entries.append({
                "item_id": item["item_id"],
                "warehouse_id": from_wh,
                "actual_qty": str(round_currency(-qty)),
                "incoming_rate": "0",
                "batch_id": item.get("batch_id"),
                "serial_number": item.get("serial_numbers"),
                "fiscal_year": fiscal_year,
            })
            sle_entries.append({
                "item_id": item["item_id"],
                "warehouse_id": to_wh,
                "actual_qty": str(round_currency(qty)),
                "incoming_rate": str(round_currency(rate)),
                "batch_id": item.get("batch_id"),
                "serial_number": item.get("serial_numbers"),
                "fiscal_year": fiscal_year,
            })
        elif entry_type == "manufacture":
            # Finished goods to to_warehouse, raw materials from from_warehouse
            if to_wh:
                sle_entries.append({
                    "item_id": item["item_id"],
                    "warehouse_id": to_wh,
                    "actual_qty": str(round_currency(qty)),
                    "incoming_rate": str(round_currency(rate)),
                    "batch_id": item.get("batch_id"),
                    "serial_number": item.get("serial_numbers"),
                    "fiscal_year": fiscal_year,
                })
            if from_wh:
                sle_entries.append({
                    "item_id": item["item_id"],
                    "warehouse_id": from_wh,
                    "actual_qty": str(round_currency(-qty)),
                    "incoming_rate": "0",
                    "batch_id": item.get("batch_id"),
                    "serial_number": item.get("serial_numbers"),
                    "fiscal_year": fiscal_year,
                })
        elif entry_type == "repack":
            # Each repack line is single-direction (enforced at add time):
            #   input line  → from_wh set → consume (negative qty)
            #   output line → to_wh set   → produce (positive qty at its rate)
            # The cost-balance invariant (input value == output value) was already
            # verified at draft, so the SLE pair nets to zero stock-value change.
            if to_wh:
                sle_entries.append({
                    "item_id": item["item_id"],
                    "warehouse_id": to_wh,
                    "actual_qty": str(round_currency(qty)),
                    "incoming_rate": str(round_currency(rate)),
                    "batch_id": item.get("batch_id"),
                    "serial_number": item.get("serial_numbers"),
                    "fiscal_year": fiscal_year,
                    # The produced item must carry a stated value, never $0.
                    "require_rate": True,
                })
            else:
                sle_entries.append({
                    "item_id": item["item_id"],
                    "warehouse_id": from_wh,
                    "actual_qty": str(round_currency(-qty)),
                    "incoming_rate": "0",
                    "batch_id": item.get("batch_id"),
                    "serial_number": item.get("serial_numbers"),
                    "fiscal_year": fiscal_year,
                })
        elif entry_type == "send_to_subcontractor":
            # Transfer OUT: negative at from_wh, positive at the subcontractor
            # sub-store (to_wh was forced to the validated supplier warehouse at
            # add time). The sub-store leg inherits the source valuation.
            sle_entries.append({
                "item_id": item["item_id"],
                "warehouse_id": from_wh,
                "actual_qty": str(round_currency(-qty)),
                "incoming_rate": "0",
                "batch_id": item.get("batch_id"),
                "serial_number": item.get("serial_numbers"),
                "fiscal_year": fiscal_year,
            })
            sle_entries.append({
                "item_id": item["item_id"],
                "warehouse_id": to_wh,
                "actual_qty": str(round_currency(qty)),
                "incoming_rate": str(round_currency(rate)),
                "batch_id": item.get("batch_id"),
                "serial_number": item.get("serial_numbers"),
                "fiscal_year": fiscal_year,
            })
        elif entry_type == "material_consumption":
            # Issue raw materials against the work order: negative at from_wh.
            sle_entries.append({
                "item_id": item["item_id"],
                "warehouse_id": from_wh,
                "actual_qty": str(round_currency(-qty)),
                "incoming_rate": "0",
                "batch_id": item.get("batch_id"),
                "serial_number": item.get("serial_numbers"),
                "fiscal_year": fiscal_year,
            })

    # Hard-reservation enforcement (ADR-0026): a material_issue can never drive
    # available stock below the sum of that warehouse's ACTIVE reservations for
    # the item. available = actual_qty - SUM(active reserved_qty). Computed and
    # blocked BEFORE any SLE is written (single-transaction; full rollback). This
    # is what makes reservations "hard" rather than a cosmetic column.
    if entry_type == "material_issue":
        issue_per_key = {}  # (item_id, from_warehouse_id) -> total issue qty
        for item_row in items:
            il = row_to_dict(item_row)
            key = (il["item_id"], il.get("from_warehouse_id"))
            issue_per_key[key] = issue_per_key.get(key, Decimal("0")) + to_decimal(il["quantity"])
        for (item_id, from_warehouse_id), issue_qty in issue_per_key.items():
            if not from_warehouse_id:
                continue
            available = _available_qty(conn, item_id, from_warehouse_id)
            if issue_qty > available:
                err(f"Cannot issue {issue_qty} of item {item_id} from warehouse "
                    f"{from_warehouse_id}: only {available} available (actual minus "
                    f"active reservations). Release a reservation first.")

    # Insert SLE entries via shared lib
    try:
        sle_ids = insert_sle_entries(
            conn, sle_entries,
            voucher_type="stock_entry",
            voucher_id=args.stock_entry_id,
            posting_date=posting_date,
            company_id=company_id,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err(f"SLE posting failed: {e}")

    # Build SLE dicts with stock_value_difference for GL generation
    sle_t = Table("stock_ledger_entry")
    sle_q = (Q.from_(sle_t).select(sle_t.star)
             .where(sle_t.voucher_type == "stock_entry")
             .where(sle_t.voucher_id == P())
             .where(sle_t.is_cancelled == 0))
    sle_rows = conn.execute(sle_q.get_sql(), (args.stock_entry_id,)).fetchall()
    sle_dicts = [row_to_dict(r) for r in sle_rows]

    # Create perpetual inventory GL entries
    try:
        gl_entries = create_perpetual_inventory_gl(
            conn, sle_dicts,
            voucher_type="stock_entry",
            voucher_id=args.stock_entry_id,
            posting_date=posting_date,
            company_id=company_id,
            cost_center_id=cost_center_id,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err(f"GL posting failed: {e}")

    gl_ids = []
    if gl_entries:
        # Add fiscal_year to each GL entry
        for gle in gl_entries:
            gle["fiscal_year"] = fiscal_year
        try:
            gl_ids = insert_gl_entries(
                conn, gl_entries,
                voucher_type="stock_entry",
                voucher_id=args.stock_entry_id,
                posting_date=posting_date,
                company_id=company_id,
                remarks=f"Stock Entry {se_dict['naming_series']}",
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-inventory] {e}\n")
            err(f"GL posting failed: {e}")

    # Update status
    conn.execute(
        "UPDATE stock_entry SET status = 'submitted', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (args.stock_entry_id,),
    )

    audit(conn, "erpclaw-inventory", "submit-stock-entry", "stock_entry", args.stock_entry_id,
           new_values={"sle_count": len(sle_ids), "gl_count": len(gl_ids)})
    conn.commit()

    ok({"stock_entry_id": args.stock_entry_id,
         "sle_entries_created": len(sle_ids),
         "gl_entries_created": len(gl_ids)})


# ---------------------------------------------------------------------------
# 14. cancel-stock-entry
# ---------------------------------------------------------------------------

def cancel_stock_entry(conn, args):
    """Cancel a submitted stock entry."""
    if not args.stock_entry_id:
        err("--stock-entry-id is required")

    se_t = Table("stock_entry")
    se_q = Q.from_(se_t).select(se_t.star).where(se_t.id == P())
    se = conn.execute(se_q.get_sql(), (args.stock_entry_id,)).fetchone()
    if not se:
        err(f"Stock entry {args.stock_entry_id} not found")
    if se["status"] != "submitted":
        err(f"Cannot cancel: stock entry is '{se['status']}' (must be 'submitted')",
             suggestion="Only submitted stock entries can be cancelled.")

    posting_date = se["posting_date"]

    # Reverse SLE entries
    try:
        reversal_sle_ids = reverse_sle_entries(
            conn,
            voucher_type="stock_entry",
            voucher_id=args.stock_entry_id,
            posting_date=posting_date,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err(f"SLE reversal failed: {e}")

    # Reverse GL entries
    try:
        reversal_gl_ids = reverse_gl_entries(
            conn,
            voucher_type="stock_entry",
            voucher_id=args.stock_entry_id,
            posting_date=posting_date,
        )
    except ValueError:
        # GL entries may not exist if perpetual inventory GL was skipped
        reversal_gl_ids = []

    # Update status
    conn.execute(
        "UPDATE stock_entry SET status = 'cancelled', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (args.stock_entry_id,),
    )

    audit(conn, "erpclaw-inventory", "cancel-stock-entry", "stock_entry", args.stock_entry_id,
           new_values={"reversed": True})
    conn.commit()

    ok({"stock_entry_id": args.stock_entry_id, "reversed": True,
         "sle_reversals": len(reversal_sle_ids),
         "gl_reversals": len(reversal_gl_ids)})


# ---------------------------------------------------------------------------
# 15. create-stock-ledger-entries (cross-skill)
# ---------------------------------------------------------------------------

def create_stock_ledger_entries(conn, args):
    """Cross-skill: create SLE entries (called by selling/buying)."""
    if not args.voucher_type:
        err("--voucher-type is required")
    # FINDING-006: canonicalize the doctype voucher_type at the gateway boundary
    # so stock_ledger_entry.voucher_type is stored snake_case (a label like
    # "Delivery Note" would otherwise break every downstream filter).
    voucher_type = canonical_voucher_type(args.voucher_type)
    if not args.voucher_id:
        err("--voucher-id is required")
    if not args.posting_date:
        err("--posting-date is required")
    if not args.entries:
        err("--entries is required (JSON array)")
    if not args.company_id:
        err("--company-id is required")

    entries = _parse_json_arg(args.entries, "entries")
    if not entries or not isinstance(entries, list):
        err("--entries must be a non-empty JSON array")

    fiscal_year = _get_fiscal_year(conn, args.posting_date)

    # Add fiscal_year to each entry
    for entry in entries:
        entry["fiscal_year"] = fiscal_year

    try:
        sle_ids = insert_sle_entries(
            conn, entries,
            voucher_type=voucher_type,
            voucher_id=args.voucher_id,
            posting_date=args.posting_date,
            company_id=args.company_id,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err(f"SLE posting failed: {e}")

    audit(conn, "erpclaw-inventory", "create-stock-ledger-entries", "stock_ledger_entry",
           args.voucher_id,
           new_values={"voucher_type": voucher_type,
                       "sle_count": len(sle_ids)})
    conn.commit()
    ok({"sle_ids": sle_ids, "count": len(sle_ids)})


# ---------------------------------------------------------------------------
# 16. reverse-stock-ledger-entries (cross-skill)
# ---------------------------------------------------------------------------

def reverse_stock_ledger_entries(conn, args):
    """Cross-skill: reverse SLE entries (called by selling/buying)."""
    if not args.voucher_type:
        err("--voucher-type is required")
    # FINDING-006: normalize so the reversal lookup matches the canonical
    # voucher_type the SLE rows were stored under.
    voucher_type = canonical_voucher_type(args.voucher_type)
    if not args.voucher_id:
        err("--voucher-id is required")
    if not args.posting_date:
        err("--posting-date is required")

    try:
        reversal_ids = reverse_sle_entries(
            conn,
            voucher_type=voucher_type,
            voucher_id=args.voucher_id,
            posting_date=args.posting_date,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err(f"SLE reversal failed: {e}")

    audit(conn, "erpclaw-inventory", "reverse-stock-ledger-entries", "stock_ledger_entry",
           args.voucher_id,
           new_values={"voucher_type": voucher_type,
                       "reversal_count": len(reversal_ids)})
    conn.commit()
    ok({"reversal_ids": reversal_ids, "count": len(reversal_ids)})


# ---------------------------------------------------------------------------
# 17. get-stock-balance
# ---------------------------------------------------------------------------

def get_stock_balance_action(conn, args):
    """Get stock balance for an item in a warehouse."""
    if not args.item_id:
        err("--item-id is required")
    if not args.warehouse_id:
        err("--warehouse-id is required")

    balance = get_stock_balance(conn, args.item_id, args.warehouse_id)
    ok({"item_id": args.item_id, "warehouse_id": args.warehouse_id,
         "qty": balance["qty"], "valuation_rate": balance["valuation_rate"],
         "stock_value": balance["stock_value"]})


# ---------------------------------------------------------------------------
# 18. stock-balance-report
# ---------------------------------------------------------------------------

def stock_balance_report(conn, args):
    """All items stock summary for a company."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    # This query uses decimal_sum() aggregate and a correlated subquery for
    # valuation_rate — kept as raw SQL due to complexity of correlated subquery
    # and HAVING clause with decimal_sum()
    conditions = [
        "sle.is_cancelled = 0",
        "w.company_id = ?",
    ]
    params = [company_id]

    if args.warehouse_id:
        conditions.append("sle.warehouse_id = ?")
        params.append(args.warehouse_id)

    where = " AND ".join(conditions)

    rows = conn.execute(
        f"""SELECT sle.item_id, sle.warehouse_id,
               i.item_code, i.item_name, w.name AS warehouse_name,
               decimal_sum(sle.actual_qty) AS balance_qty,
               COALESCE(
                   (SELECT valuation_rate FROM stock_ledger_entry s2
                    WHERE s2.item_id = sle.item_id AND s2.warehouse_id = sle.warehouse_id
                      AND s2.is_cancelled = 0
                    ORDER BY {latest_insert_order("s2.")} LIMIT 1),
                   '0'
               ) AS valuation_rate
           FROM stock_ledger_entry sle
           JOIN item i ON i.id = sle.item_id
           JOIN warehouse w ON w.id = sle.warehouse_id
           WHERE {where}
           GROUP BY sle.item_id, sle.warehouse_id
           HAVING decimal_sum(sle.actual_qty) + 0 != 0
           ORDER BY i.item_name, w.name""",
        params,
    ).fetchall()

    report = []
    total_value = Decimal("0")
    for row in rows:
        qty = to_decimal(str(row["balance_qty"]))
        rate = to_decimal(str(row["valuation_rate"]))
        value = round_currency(qty * rate)
        total_value += value
        report.append({
            "item_id": row["item_id"],
            "item_code": row["item_code"],
            "item_name": row["item_name"],
            "warehouse_id": row["warehouse_id"],
            "warehouse_name": row["warehouse_name"],
            "qty": str(round_currency(qty)),
            "valuation_rate": str(round_currency(rate)),
            "stock_value": str(value),
        })

    ok({"report": report, "total_stock_value": str(round_currency(total_value)),
         "row_count": len(report)})


# ---------------------------------------------------------------------------
# 19. stock-ledger-report
# ---------------------------------------------------------------------------

def stock_ledger_report(conn, args):
    """Stock ledger entry detail report."""
    sle = Table("stock_ledger_entry").as_("sle")
    i = Table("item").as_("i")
    w = Table("warehouse").as_("w")

    rows_q = (Q.from_(sle)
              .left_join(i).on(i.id == sle.item_id)
              .left_join(w).on(w.id == sle.warehouse_id)
              .select(sle.star, i.item_code, i.item_name, w.name.as_("warehouse_name"))
              .where(sle.is_cancelled == 0)
              .orderby(sle.posting_date, order=Order.desc)
              .orderby(sle.created_at, order=Order.desc)
              .limit(P()).offset(P()))

    params = []
    if args.item_id:
        rows_q = rows_q.where(sle.item_id == P())
        params.append(args.item_id)
    if args.warehouse_id:
        rows_q = rows_q.where(sle.warehouse_id == P())
        params.append(args.warehouse_id)
    if args.from_date:
        rows_q = rows_q.where(sle.posting_date >= P())
        params.append(args.from_date)
    if args.to_date:
        rows_q = rows_q.where(sle.posting_date <= P())
        params.append(args.to_date)

    limit = int(args.limit) if args.limit else 100
    offset = int(args.offset) if args.offset else 0
    params.extend([limit, offset])

    rows = conn.execute(rows_q.get_sql(), params).fetchall()

    ok({"entries": [row_to_dict(r) for r in rows], "count": len(rows)})


# ---------------------------------------------------------------------------
# 20. add-batch
# ---------------------------------------------------------------------------

def add_batch(conn, args):
    """Create a batch."""
    if not args.item_id:
        err("--item-id is required")
    if not args.batch_name:
        err("--batch-name is required")

    item_t = Table("item")
    item_q = (Q.from_(item_t)
              .select(item_t.id, item_t.has_batch)
              .where(item_t.id == P()))
    item = conn.execute(item_q.get_sql(), (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")

    batch_id = str(uuid.uuid4())
    t = Table("batch")
    q = Q.into(t).columns(
        "id", "batch_name", "item_id", "manufacturing_date", "expiry_date",
    ).insert(P(), P(), P(), P(), P())
    try:
        conn.execute(
            q.get_sql(),
            (batch_id, args.batch_name, args.item_id,
             args.manufacturing_date, args.expiry_date),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err("Batch creation failed — check for duplicates or invalid data")

    audit(conn, "erpclaw-inventory", "add-batch", "batch", batch_id,
           new_values={"batch_name": args.batch_name, "item_id": args.item_id})
    conn.commit()
    ok({"batch_id": batch_id, "batch_name": args.batch_name})


# ---------------------------------------------------------------------------
# 21. list-batches
# ---------------------------------------------------------------------------

def list_batches(conn, args):
    """List batches with optional filters."""
    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0

    if args.warehouse_id:
        # Filter by batches that have stock in the specified warehouse
        # Uses decimal_sum() HAVING — kept as raw SQL
        conditions = ["1=1"]
        params = []
        if args.item_id:
            conditions.append("b.item_id = ?")
            params.append(args.item_id)
        where = " AND ".join(conditions)

        count_row = conn.execute(
            f"""SELECT COUNT(*) FROM (
                   SELECT b.id
                   FROM batch b
                   JOIN stock_ledger_entry sle ON sle.batch_id = b.id
                   WHERE {where} AND sle.warehouse_id = ? AND sle.is_cancelled = 0
                   GROUP BY b.id
                   HAVING decimal_sum(sle.actual_qty) + 0 > 0
               )""",
            params + [args.warehouse_id],
        ).fetchone()
        total_count = count_row[0]

        rows = conn.execute(
            f"""SELECT DISTINCT b.*
               FROM batch b
               JOIN stock_ledger_entry sle ON sle.batch_id = b.id
               WHERE {where} AND sle.warehouse_id = ? AND sle.is_cancelled = 0
               GROUP BY b.id
               HAVING decimal_sum(sle.actual_qty) + 0 > 0
               ORDER BY b.batch_name
               LIMIT ? OFFSET ?""",
            params + [args.warehouse_id, limit, offset],
        ).fetchall()
    else:
        b = Table("batch").as_("b")

        count_q = Q.from_(b).select(fn.Count("*"))
        if args.item_id:
            count_q = count_q.where(b.item_id == P())

        count_params = []
        if args.item_id:
            count_params.append(args.item_id)

        count_row = conn.execute(count_q.get_sql(), count_params).fetchone()
        total_count = count_row[0]

        rows_q = (Q.from_(b).select(b.star)
                  .orderby(b.batch_name)
                  .limit(P()).offset(P()))
        if args.item_id:
            rows_q = rows_q.where(b.item_id == P())

        row_params = []
        if args.item_id:
            row_params.append(args.item_id)
        row_params.extend([limit, offset])

        rows = conn.execute(rows_q.get_sql(), row_params).fetchall()

    ok({"batches": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 22. add-serial-number
# ---------------------------------------------------------------------------

def add_serial_number(conn, args):
    """Register a serial number."""
    if not args.item_id:
        err("--item-id is required")
    if not args.serial_no:
        err("--serial-no is required")

    item_t = Table("item")
    item_q = Q.from_(item_t).select(item_t.id).where(item_t.id == P())
    item = conn.execute(item_q.get_sql(), (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")

    sn_id = str(uuid.uuid4())
    t = Table("serial_number")
    q = Q.into(t).columns(
        "id", "serial_no", "item_id", "warehouse_id", "batch_id", "status",
    ).insert(P(), P(), P(), P(), P(), "active")
    try:
        conn.execute(
            q.get_sql(),
            (sn_id, args.serial_no, args.item_id,
             args.warehouse_id, args.batch_id),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err("Serial number creation failed — check for duplicates or invalid data")

    audit(conn, "erpclaw-inventory", "add-serial-number", "serial_number", sn_id,
           new_values={"serial_no": args.serial_no, "item_id": args.item_id})
    conn.commit()
    ok({"serial_number_id": sn_id, "serial_no": args.serial_no})


# ---------------------------------------------------------------------------
# 23. list-serial-numbers
# ---------------------------------------------------------------------------

def list_serial_numbers(conn, args):
    """List serial numbers with optional filters."""
    sn = Table("serial_number").as_("sn")
    i = Table("item").as_("i")

    count_q = Q.from_(sn).select(fn.Count("*"))
    if args.item_id:
        count_q = count_q.where(sn.item_id == P())
    if args.warehouse_id:
        count_q = count_q.where(sn.warehouse_id == P())
    if args.sn_status:
        if args.sn_status not in VALID_SERIAL_STATUSES:
            err(f"--status must be one of: {', '.join(VALID_SERIAL_STATUSES)}")
        count_q = count_q.where(sn.status == P())

    count_params = []
    if args.item_id:
        count_params.append(args.item_id)
    if args.warehouse_id:
        count_params.append(args.warehouse_id)
    if args.sn_status:
        count_params.append(args.sn_status)

    count_row = conn.execute(count_q.get_sql(), count_params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0

    rows_q = (Q.from_(sn)
              .left_join(i).on(i.id == sn.item_id)
              .select(sn.star, i.item_code, i.item_name)
              .orderby(sn.serial_no)
              .limit(P()).offset(P()))
    if args.item_id:
        rows_q = rows_q.where(sn.item_id == P())
    if args.warehouse_id:
        rows_q = rows_q.where(sn.warehouse_id == P())
    if args.sn_status:
        rows_q = rows_q.where(sn.status == P())

    row_params = []
    if args.item_id:
        row_params.append(args.item_id)
    if args.warehouse_id:
        row_params.append(args.warehouse_id)
    if args.sn_status:
        row_params.append(args.sn_status)
    row_params.extend([limit, offset])

    rows = conn.execute(rows_q.get_sql(), row_params).fetchall()

    ok({"serial_numbers": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 24. add-price-list
# ---------------------------------------------------------------------------

def add_price_list(conn, args):
    """Create a price list."""
    if not args.name:
        err("--name is required")

    pl_id = str(uuid.uuid4())
    currency = args.currency or "USD"
    is_buying = int(args.is_buying) if args.is_buying else 0
    is_selling = int(args.is_selling) if args.is_selling else 0

    t = Table("price_list")
    q = Q.into(t).columns("id", "name", "currency", "buying", "selling").insert(P(), P(), P(), P(), P())
    try:
        conn.execute(q.get_sql(), (pl_id, args.name, currency, is_buying, is_selling))
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err("Price list creation failed — check for duplicates or invalid data")

    audit(conn, "erpclaw-inventory", "add-price-list", "price_list", pl_id,
           new_values={"name": args.name})
    conn.commit()
    ok({"price_list_id": pl_id, "name": args.name})


# ---------------------------------------------------------------------------
# 25. add-item-price
# ---------------------------------------------------------------------------

def add_item_price(conn, args):
    """Set a price for an item in a price list."""
    if not args.item_id:
        err("--item-id is required")
    if not args.price_list_id:
        err("--price-list-id is required")
    if not args.rate:
        err("--rate is required")

    # Validate references
    item_t = Table("item")
    item_q = Q.from_(item_t).select(item_t.id).where(item_t.id == P())
    if not conn.execute(item_q.get_sql(), (args.item_id,)).fetchone():
        err(f"Item {args.item_id} not found")

    pl_t = Table("price_list")
    pl_q = Q.from_(pl_t).select(pl_t.id).where(pl_t.id == P())
    if not conn.execute(pl_q.get_sql(), (args.price_list_id,)).fetchone():
        err(f"Price list {args.price_list_id} not found")

    rate = round_currency(to_decimal(args.rate))
    min_qty = str(to_decimal(args.min_qty or "0"))

    ip_id = str(uuid.uuid4())
    t = Table("item_price")
    q = Q.into(t).columns(
        "id", "item_id", "price_list_id", "rate", "min_qty", "valid_from", "valid_to",
    ).insert(P(), P(), P(), P(), P(), P(), P())
    conn.execute(
        q.get_sql(),
        (ip_id, args.item_id, args.price_list_id, str(rate),
         min_qty, args.valid_from, args.valid_to),
    )

    audit(conn, "erpclaw-inventory", "add-item-price", "item_price", ip_id,
           new_values={"item_id": args.item_id, "rate": str(rate)})
    conn.commit()
    ok({"item_price_id": ip_id, "rate": str(rate)})


# ---------------------------------------------------------------------------
# 26. get-item-price
# ---------------------------------------------------------------------------

def get_item_price(conn, args):
    """Get applicable price for an item from a price list."""
    if not args.item_id:
        err("--item-id is required")
    if not args.price_list_id:
        err("--price-list-id is required")

    qty = to_decimal(args.qty or "1")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Find best matching price: valid date range, min_qty <= requested qty
    # Order by min_qty DESC to get the most specific tier first
    # This query uses IS NULL comparisons — kept as raw SQL (rule 16)
    rows = conn.execute(
        """SELECT * FROM item_price
           WHERE item_id = ? AND price_list_id = ?
             AND CAST(min_qty AS NUMERIC) <= CAST(? AS NUMERIC)
             AND (valid_from IS NULL OR valid_from <= ?)
             AND (valid_to IS NULL OR valid_to >= ?)
           ORDER BY CAST(min_qty AS NUMERIC) DESC
           LIMIT 1""",
        (args.item_id, args.price_list_id, str(qty), today, today),
    ).fetchone()

    if not rows:
        # Fallback: any price for this item/price list (ignoring date/qty)
        ip_t = Table("item_price")
        fallback_q = (Q.from_(ip_t).select(ip_t.star)
                      .where(ip_t.item_id == P())
                      .where(ip_t.price_list_id == P())
                      .orderby(ip_t.created_at, order=Order.desc)
                      .limit(1))
        rows = conn.execute(
            fallback_q.get_sql(),
            (args.item_id, args.price_list_id),
        ).fetchone()

    if not rows:
        err(f"No price found for item {args.item_id} in price list {args.price_list_id}")

    data = row_to_dict(rows)
    ok(data)


# ---------------------------------------------------------------------------
# 27. add-pricing-rule
# ---------------------------------------------------------------------------

def add_pricing_rule(conn, args):
    """Create a pricing/discount rule."""
    if not args.name:
        err("--name is required")
    if not args.applies_to:
        err("--applies-to is required (item|item_group|customer|customer_group)")
    if args.applies_to not in ("item", "item_group", "customer", "customer_group"):
        err("--applies-to must be item|item_group|customer|customer_group")
    if not args.company_id:
        err("--company-id is required")

    pr_id = str(uuid.uuid4())
    t = Table("pricing_rule")
    q = Q.into(t).columns(
        "id", "name", "applies_to", "entity_id", "discount_percentage", "rate",
        "min_qty", "max_qty", "valid_from", "valid_to", "priority", "company_id",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P())
    conn.execute(
        q.get_sql(),
        (pr_id, args.name, args.applies_to, args.entity_id,
         args.discount_percentage, args.pr_rate,
         args.min_qty, args.max_qty,
         args.valid_from, args.valid_to,
         args.priority or 0, args.company_id),
    )

    audit(conn, "erpclaw-inventory", "add-pricing-rule", "pricing_rule", pr_id,
           new_values={"name": args.name, "applies_to": args.applies_to})
    conn.commit()
    ok({"pricing_rule_id": pr_id, "name": args.name})


# ---------------------------------------------------------------------------
# 28. add-stock-reconciliation
# ---------------------------------------------------------------------------

def add_stock_reconciliation(conn, args):
    """Create a stock reconciliation (physical count)."""
    if not args.posting_date:
        err("--posting-date is required")
    if not args.items:
        err("--items is required (JSON array)")
    if not args.company_id:
        err("--company-id is required")

    co_t = Table("company")
    co_q = Q.from_(co_t).select(co_t.id).where(co_t.id == P())
    if not conn.execute(co_q.get_sql(), (args.company_id,)).fetchone():
        err(f"Company {args.company_id} not found")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    sr_id = str(uuid.uuid4())
    naming = get_next_name(conn, "stock_reconciliation",
                           company_id=args.company_id)

    total_diff_amount = Decimal("0")

    # Validate and collect item rows before inserting
    item_rows_to_insert = []
    for i, item in enumerate(items):
        item_id = item.get("item_id")
        warehouse_id = item.get("warehouse_id")
        if not item_id:
            err(f"Item {i}: item_id is required")
        if not warehouse_id:
            err(f"Item {i}: warehouse_id is required")

        # Get current stock balance
        balance = get_stock_balance(conn, item_id, warehouse_id)
        current_qty = to_decimal(balance["qty"])
        current_rate = to_decimal(balance["valuation_rate"])

        counted_qty = to_decimal(item.get("qty", "0"))
        counted_rate = to_decimal(item.get("valuation_rate", str(current_rate)))

        qty_diff = round_currency(counted_qty - current_qty)
        current_value = round_currency(current_qty * current_rate)
        counted_value = round_currency(counted_qty * counted_rate)
        amount_diff = round_currency(counted_value - current_value)
        total_diff_amount += amount_diff

        item_rows_to_insert.append((
            str(uuid.uuid4()), sr_id, item_id, warehouse_id,
            str(round_currency(current_qty)), str(round_currency(current_rate)),
            str(round_currency(counted_qty)), str(round_currency(counted_rate)),
            str(qty_diff), str(amount_diff),
        ))

    # Insert parent stock_reconciliation first (FK target for items)
    sr_t = Table("stock_reconciliation")
    sr_q = Q.into(sr_t).columns(
        "id", "naming_series", "posting_date", "difference_amount",
        "status", "company_id",
    ).insert(P(), P(), P(), P(), "draft", P())
    conn.execute(
        sr_q.get_sql(),
        (sr_id, naming, args.posting_date,
         str(round_currency(total_diff_amount)), args.company_id),
    )

    # Now insert child stock_reconciliation_item rows
    sri_t = Table("stock_reconciliation_item")
    sri_q = Q.into(sri_t).columns(
        "id", "stock_reconciliation_id", "item_id", "warehouse_id",
        "current_qty", "current_valuation_rate", "qty", "valuation_rate",
        "quantity_difference", "amount_difference",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P())
    for row_params in item_rows_to_insert:
        conn.execute(sri_q.get_sql(), row_params)

    audit(conn, "erpclaw-inventory", "add-stock-reconciliation", "stock_reconciliation", sr_id,
           new_values={"naming_series": naming, "item_count": len(items),
                       "difference_amount": str(round_currency(total_diff_amount))})
    conn.commit()
    ok({"stock_reconciliation_id": sr_id, "naming_series": naming,
         "difference_amount": str(round_currency(total_diff_amount)),
         "item_count": len(items)})


# ---------------------------------------------------------------------------
# 29. submit-stock-reconciliation
# ---------------------------------------------------------------------------

def submit_stock_reconciliation(conn, args):
    """Submit a stock reconciliation: post SLE + GL for differences."""
    if not args.stock_reconciliation_id:
        err("--stock-reconciliation-id is required")

    sr_t = Table("stock_reconciliation")
    sr_q = Q.from_(sr_t).select(sr_t.star).where(sr_t.id == P())
    sr = conn.execute(sr_q.get_sql(), (args.stock_reconciliation_id,)).fetchone()
    if not sr:
        err(f"Stock reconciliation {args.stock_reconciliation_id} not found")
    if sr["status"] != "draft":
        err(f"Cannot submit: reconciliation is '{sr['status']}' (must be 'draft')")

    sr_dict = row_to_dict(sr)
    company_id = sr_dict["company_id"]
    posting_date = sr_dict["posting_date"]

    # Fetch reconciliation items
    sri_t = Table("stock_reconciliation_item")
    sri_q = (Q.from_(sri_t).select(sri_t.star)
             .where(sri_t.stock_reconciliation_id == P()))
    sri_rows = conn.execute(sri_q.get_sql(), (args.stock_reconciliation_id,)).fetchall()
    if not sri_rows:
        err("Stock reconciliation has no items")

    fiscal_year = _get_fiscal_year(conn, posting_date)
    cost_center_id = _get_cost_center(conn, company_id)

    # Build SLE entries for quantity differences
    sle_entries = []
    for sri in sri_rows:
        item = row_to_dict(sri)
        qty_diff = to_decimal(item["quantity_difference"])
        if qty_diff == 0:
            continue

        valuation_rate = to_decimal(item["valuation_rate"])
        sle_entries.append({
            "item_id": item["item_id"],
            "warehouse_id": item["warehouse_id"],
            "actual_qty": str(round_currency(qty_diff)),
            "incoming_rate": str(round_currency(valuation_rate)) if qty_diff > 0 else "0",
            "fiscal_year": fiscal_year,
        })

    sle_ids = []
    if sle_entries:
        try:
            sle_ids = insert_sle_entries(
                conn, sle_entries,
                voucher_type="stock_reconciliation",
                voucher_id=args.stock_reconciliation_id,
                posting_date=posting_date,
                company_id=company_id,
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-inventory] {e}\n")
            err(f"SLE posting failed: {e}")

    # Build GL entries for value adjustments
    gl_ids = []
    if sle_ids:
        sle_rows_t = Table("stock_ledger_entry")
        sle_rows_q = (Q.from_(sle_rows_t).select(sle_rows_t.star)
                      .where(sle_rows_t.voucher_type == "stock_reconciliation")
                      .where(sle_rows_t.voucher_id == P())
                      .where(sle_rows_t.is_cancelled == 0))
        sle_rows = conn.execute(sle_rows_q.get_sql(), (args.stock_reconciliation_id,)).fetchall()
        sle_dicts = [row_to_dict(r) for r in sle_rows]

        # Find stock adjustment account as contra for reconciliation
        # Uses account_type filter — kept as PyPika
        acct_t = Table("account")
        acct_q = (Q.from_(acct_t).select(acct_t.id)
                  .where(acct_t.account_type == "stock_adjustment")
                  .where(acct_t.company_id == P())
                  .where(acct_t.is_group == 0)
                  .limit(1))
        stock_adj_acct = conn.execute(acct_q.get_sql(), (company_id,)).fetchone()
        expense_account_id = stock_adj_acct["id"] if stock_adj_acct else None

        try:
            gl_entries = create_perpetual_inventory_gl(
                conn, sle_dicts,
                voucher_type="stock_reconciliation",
                voucher_id=args.stock_reconciliation_id,
                posting_date=posting_date,
                company_id=company_id,
                expense_account_id=expense_account_id,
                cost_center_id=cost_center_id,
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-inventory] {e}\n")
            err(f"GL posting failed: {e}")

        if gl_entries:
            for gle in gl_entries:
                gle["fiscal_year"] = fiscal_year
            try:
                gl_ids = insert_gl_entries(
                    conn, gl_entries,
                    voucher_type="stock_reconciliation",
                    voucher_id=args.stock_reconciliation_id,
                    posting_date=posting_date,
                    company_id=company_id,
                    remarks=f"Stock Reconciliation {sr_dict['naming_series']}",
                )
            except ValueError as e:
                sys.stderr.write(f"[erpclaw-inventory] {e}\n")
                err(f"GL posting failed: {e}")

    # Update status
    conn.execute(
        "UPDATE stock_reconciliation SET status = 'submitted', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (args.stock_reconciliation_id,),
    )

    audit(conn, "erpclaw-inventory", "submit-stock-reconciliation", "stock_reconciliation",
           args.stock_reconciliation_id,
           new_values={"sle_count": len(sle_ids), "gl_count": len(gl_ids)})
    conn.commit()

    ok({"stock_reconciliation_id": args.stock_reconciliation_id,
         "sle_entries_created": len(sle_ids),
         "gl_entries_created": len(gl_ids)})


# ---------------------------------------------------------------------------
# 30. revalue-stock
# ---------------------------------------------------------------------------

def revalue_stock(conn, args):
    """Revalue inventory for an item in a warehouse.

    Changes the valuation rate without affecting quantity. Creates:
    - SLE entry with actual_qty=0 recording the new rate
    - GL entries adjusting stock value (Stock-in-Hand vs Stock-Adjustment)
    - Audit trail in stock_revaluation table

    This is a one-step action: no draft state, posts immediately.
    """
    item_id = args.item_id
    warehouse_id = args.warehouse_id
    new_rate = args.new_rate
    posting_date = args.posting_date
    reason = args.reason

    if not item_id:
        err("--item-id is required")
    if not warehouse_id:
        err("--warehouse-id is required")
    if not new_rate:
        err("--new-rate is required")
    if not posting_date:
        err("--posting-date is required")

    # Validate new_rate
    try:
        new_rate_d = to_decimal(new_rate)
    except (InvalidOperation, ValueError):
        err(f"Invalid --new-rate: {new_rate}")
    if new_rate_d < 0:
        err("--new-rate must be non-negative")

    # Validate item exists and is a stock item
    item_t = Table("item")
    item_q = (Q.from_(item_t)
              .select(item_t.id, item_t.item_code, item_t.item_name, item_t.is_stock_item)
              .where(item_t.id == P()))
    item_row = conn.execute(item_q.get_sql(), (item_id,)).fetchone()
    if not item_row:
        err(f"Item {item_id} not found")
    if not item_row["is_stock_item"]:
        err(f"Item {item_row['item_name']} is not a stock item")

    # Validate warehouse
    wh_t = Table("warehouse")
    wh_q = (Q.from_(wh_t)
            .select(wh_t.id, wh_t.name, wh_t.company_id, wh_t.account_id)
            .where(wh_t.id == P()))
    wh_row = conn.execute(wh_q.get_sql(), (warehouse_id,)).fetchone()
    if not wh_row:
        err(f"Warehouse {warehouse_id} not found")
    company_id = wh_row["company_id"]

    # Get current stock balance
    balance = get_stock_balance(conn, item_id, warehouse_id)
    current_qty = to_decimal(balance["qty"])
    old_rate_d = to_decimal(balance["valuation_rate"])
    old_value = to_decimal(balance["stock_value"])

    if current_qty <= 0:
        err(f"Cannot revalue: no stock on hand for item '{item_row['item_name']}' "
            f"in warehouse '{wh_row['name']}' (qty={current_qty})")

    if new_rate_d == old_rate_d:
        err(f"New rate ({new_rate_d}) is the same as current rate ({old_rate_d}). No revaluation needed.")

    # Compute adjustment
    new_value = round_currency(current_qty * new_rate_d)
    adjustment = round_currency(new_value - old_value)

    fiscal_year = _get_fiscal_year(conn, posting_date)
    cost_center_id = _get_cost_center(conn, company_id)

    # Generate IDs
    reval_id = str(uuid.uuid4())

    # Naming series
    naming = get_next_name(conn, "stock_revaluation", company_id=company_id)

    # --- Single atomic transaction ---

    # 1. Insert the zero-qty valuation SLE row through the shared repricing helper.
    # For moving-average items this is byte-identical to the historical inline
    # INSERT; for FIFO items the helper ALSO resets the open layer rates to the new
    # rate (R1: revaluation previously skipped FIFO layers, so FIFO stock kept
    # consuming at stale costs). The GL side is unchanged and posted below.
    reprice_stock_valuation(
        conn, item_id, warehouse_id,
        voucher_type="stock_revaluation",
        voucher_id=reval_id,
        posting_date=posting_date,
        new_rate=str(new_rate_d),
        fiscal_year=fiscal_year,
    )

    # 2. Create GL entries for the value adjustment
    gl_ids = []
    if adjustment != 0:
        # Stock-in-Hand account (from warehouse)
        warehouse_account_id = wh_row["account_id"]
        if not warehouse_account_id:
            stock_acct_t = Table("account")
            stock_acct_q = (Q.from_(stock_acct_t).select(stock_acct_t.id)
                            .where(stock_acct_t.account_type == "stock")
                            .where(stock_acct_t.company_id == P())
                            .where(stock_acct_t.is_group == 0)
                            .limit(1))
            stock_acct = conn.execute(stock_acct_q.get_sql(), (company_id,)).fetchone()
            warehouse_account_id = stock_acct["id"] if stock_acct else None

        # Stock Adjustment account (contra)
        adj_acct_t = Table("account")
        adj_acct_q = (Q.from_(adj_acct_t).select(adj_acct_t.id)
                      .where(adj_acct_t.account_type == "stock_adjustment")
                      .where(adj_acct_t.company_id == P())
                      .where(adj_acct_t.is_group == 0)
                      .limit(1))
        stock_adj_acct = conn.execute(adj_acct_q.get_sql(), (company_id,)).fetchone()
        stock_adj_account_id = stock_adj_acct["id"] if stock_adj_acct else None

        if warehouse_account_id and stock_adj_account_id:
            abs_adj = abs(adjustment)
            gl_entries = []
            if adjustment > 0:
                # Rate increased: DR Stock-in-Hand, CR Stock Adjustment
                gl_entries.append({
                    "account_id": warehouse_account_id,
                    "debit": str(round_currency(abs_adj)),
                    "credit": "0",
                })
                gl_entries.append({
                    "account_id": stock_adj_account_id,
                    "debit": "0",
                    "credit": str(round_currency(abs_adj)),
                    "cost_center_id": cost_center_id,
                })
            else:
                # Rate decreased: DR Stock Adjustment, CR Stock-in-Hand
                gl_entries.append({
                    "account_id": stock_adj_account_id,
                    "debit": str(round_currency(abs_adj)),
                    "credit": "0",
                    "cost_center_id": cost_center_id,
                })
                gl_entries.append({
                    "account_id": warehouse_account_id,
                    "debit": "0",
                    "credit": str(round_currency(abs_adj)),
                })

            for gle in gl_entries:
                gle["fiscal_year"] = fiscal_year

            try:
                gl_ids = insert_gl_entries(
                    conn, gl_entries,
                    voucher_type="stock_revaluation",
                    voucher_id=reval_id,
                    posting_date=posting_date,
                    company_id=company_id,
                    remarks=f"Stock Revaluation {naming}: "
                            f"{item_row['item_name']} rate {old_rate_d} → {new_rate_d}",
                )
            except ValueError as e:
                sys.stderr.write(f"[erpclaw-inventory] GL posting failed: {e}\n")
                err(f"GL posting failed: {e}")

    # 3. Insert stock_revaluation record
    # Uses CAST(CURRENT_TIMESTAMP AS TEXT) for created_at and updated_at — kept as raw SQL
    conn.execute(
        """INSERT INTO stock_revaluation (
            id, naming_series, company_id, item_id, warehouse_id,
            posting_date, current_qty, old_rate, new_rate,
            adjustment_amount, reason, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted',
                  CAST(CURRENT_TIMESTAMP AS TEXT), CAST(CURRENT_TIMESTAMP AS TEXT))""",
        (
            reval_id, naming, company_id, item_id, warehouse_id,
            posting_date,
            str(round_currency(current_qty)),
            str(round_currency(old_rate_d)),
            str(round_currency(new_rate_d)),
            str(adjustment),
            reason,
        ),
    )

    audit(conn, "erpclaw-inventory", "revalue-stock", "stock_revaluation",
          reval_id, new_values={
              "item_id": item_id, "warehouse_id": warehouse_id,
              "old_rate": str(old_rate_d), "new_rate": str(new_rate_d),
              "adjustment": str(adjustment), "gl_count": len(gl_ids),
          })
    conn.commit()

    ok({
        "revaluation_id": reval_id,
        "naming_series": naming,
        "item_id": item_id,
        "item_name": item_row["item_name"],
        "warehouse": wh_row["name"],
        "current_qty": str(round_currency(current_qty)),
        "old_rate": str(round_currency(old_rate_d)),
        "new_rate": str(round_currency(new_rate_d)),
        "adjustment_amount": str(adjustment),
        "gl_entries_created": len(gl_ids),
    })


# ---------------------------------------------------------------------------
# 31. list-stock-revaluations
# ---------------------------------------------------------------------------

def list_stock_revaluations(conn, args):
    """List stock revaluations for a company."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    limit = int(args.limit or "20")
    offset = int(args.offset or "0")

    sr = Table("stock_revaluation").as_("sr")
    i = Table("item").as_("i")
    w = Table("warehouse").as_("w")

    rows_q = (Q.from_(sr)
              .join(i).on(i.id == sr.item_id)
              .join(w).on(w.id == sr.warehouse_id)
              .select(sr.star, i.item_code, i.item_name, w.name.as_("warehouse_name"))
              .where(sr.company_id == P())
              .orderby(sr.created_at, order=Order.desc)
              .limit(P()).offset(P()))

    rows = conn.execute(rows_q.get_sql(), (company_id, limit, offset)).fetchall()

    total_t = Table("stock_revaluation")
    total_q = (Q.from_(total_t)
               .select(fn.Count("*").as_("cnt"))
               .where(total_t.company_id == P()))
    total = conn.execute(total_q.get_sql(), (company_id,)).fetchone()["cnt"]

    ok({
        "revaluations": [row_to_dict(r) for r in rows],
        "total_count": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    })


# ---------------------------------------------------------------------------
# 32. get-stock-revaluation
# ---------------------------------------------------------------------------

def get_stock_revaluation(conn, args):
    """Get details of a stock revaluation."""
    reval_id = args.revaluation_id
    if not reval_id:
        err("--revaluation-id is required")

    sr = Table("stock_revaluation").as_("sr")
    i = Table("item").as_("i")
    w = Table("warehouse").as_("w")

    row_q = (Q.from_(sr)
             .join(i).on(i.id == sr.item_id)
             .join(w).on(w.id == sr.warehouse_id)
             .select(sr.star, i.item_code, i.item_name, w.name.as_("warehouse_name"))
             .where(sr.id == P()))
    row = conn.execute(row_q.get_sql(), (reval_id,)).fetchone()
    if not row:
        err(f"Stock revaluation {reval_id} not found")

    result = row_to_dict(row)

    # Include SLE entries
    sle_t = Table("stock_ledger_entry")
    sle_q = (Q.from_(sle_t).select(sle_t.star)
             .where(sle_t.voucher_type == "stock_revaluation")
             .where(sle_t.voucher_id == P()))
    sle_rows = conn.execute(sle_q.get_sql(), (reval_id,)).fetchall()
    result["sle_entries"] = [row_to_dict(r) for r in sle_rows]

    # Include GL entries
    gl_t = Table("gl_entry")
    gl_q = (Q.from_(gl_t).select(gl_t.star)
            .where(gl_t.voucher_type == "stock_revaluation")
            .where(gl_t.voucher_id == P()))
    gl_rows = conn.execute(gl_q.get_sql(), (reval_id,)).fetchall()
    result["gl_entries"] = [row_to_dict(r) for r in gl_rows]

    ok(result)


# ---------------------------------------------------------------------------
# 33. cancel-stock-revaluation
# ---------------------------------------------------------------------------

def cancel_stock_revaluation(conn, args):
    """Cancel a stock revaluation: reverse SLE and GL entries."""
    reval_id = args.revaluation_id
    if not reval_id:
        err("--revaluation-id is required")

    sr_t = Table("stock_revaluation")
    sr_q = Q.from_(sr_t).select(sr_t.star).where(sr_t.id == P())
    row = conn.execute(sr_q.get_sql(), (reval_id,)).fetchone()
    if not row:
        err(f"Stock revaluation {reval_id} not found")
    if row["status"] != "submitted":
        err(f"Cannot cancel: revaluation is '{row['status']}' (must be 'submitted')")

    reval = row_to_dict(row)
    posting_date = reval["posting_date"]

    # Reverse SLE entries
    try:
        reverse_sle_entries(
            conn,
            voucher_type="stock_revaluation",
            voucher_id=reval_id,
            posting_date=posting_date,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-inventory] SLE reversal failed: {e}\n")
        err(f"SLE reversal failed: {e}")

    # Reverse GL entries
    try:
        reverse_gl_entries(
            conn,
            voucher_type="stock_revaluation",
            voucher_id=reval_id,
            posting_date=posting_date,
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-inventory] GL reversal failed: {e}\n")
        err(f"GL reversal failed: {e}")

    # Update status
    conn.execute(
        "UPDATE stock_revaluation SET status = 'cancelled', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (reval_id,),
    )

    audit(conn, "erpclaw-inventory", "cancel-stock-revaluation", "stock_revaluation",
          reval_id, new_values={"status": "cancelled"})
    conn.commit()

    ok({
        "revaluation_id": reval_id,
        "cancelled": True,
        "item_id": reval["item_id"],
        "warehouse_id": reval["warehouse_id"],
    })


# ---------------------------------------------------------------------------
# 34. status
# ---------------------------------------------------------------------------

def status_action(conn, args):
    """Inventory summary for a company."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    item_t = Table("item")
    items_q = Q.from_(item_t).select(fn.Count("*").as_("cnt"))
    items_count = conn.execute(items_q.get_sql()).fetchone()["cnt"]

    wh_t = Table("warehouse")
    wh_q = (Q.from_(wh_t).select(fn.Count("*").as_("cnt"))
            .where(wh_t.company_id == P()))
    warehouses_count = conn.execute(wh_q.get_sql(), (company_id,)).fetchone()["cnt"]

    # Stock entries by status
    se_t = Table("stock_entry")
    se_q = (Q.from_(se_t)
            .select(se_t.status, fn.Count("*").as_("cnt"))
            .where(se_t.company_id == P())
            .groupby(se_t.status))
    se_rows = conn.execute(se_q.get_sql(), (company_id,)).fetchall()
    se_counts = {"draft": 0, "submitted": 0, "cancelled": 0}
    for row in se_rows:
        se_counts[row["status"]] = row["cnt"]
    se_counts["total"] = sum(se_counts.values())

    # Total stock value using decimal_sum() aggregate
    # JOIN with warehouse for company filter — kept as raw SQL for aggregate
    sv_row = conn.execute(
        """SELECT COALESCE(decimal_sum(sle.stock_value_difference), '0') as total_value
           FROM stock_ledger_entry sle
           JOIN warehouse w ON w.id = sle.warehouse_id
           WHERE sle.is_cancelled = 0 AND w.company_id = ?""",
        (company_id,),
    ).fetchone()
    total_stock_value = round_currency(to_decimal(str(sv_row["total_value"])))

    ok({
        "items": items_count,
        "warehouses": warehouses_count,
        "stock_entries": se_counts,
        "total_stock_value": str(total_stock_value),
    })


# ---------------------------------------------------------------------------
# 35. check-reorder
# ---------------------------------------------------------------------------

def check_reorder(conn, args):
    """Find items whose current stock is at or below their reorder level."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    # Get items with a meaningful reorder_level set
    # Uses IS NULL / IS NOT NULL comparisons — kept as raw SQL (rule 16)
    items = conn.execute(
        """SELECT id, item_code, item_name, reorder_level, reorder_qty
           FROM item
           WHERE reorder_level IS NOT NULL
             AND reorder_level != ''
             AND reorder_level != '0'
             AND status = 'active'
           ORDER BY item_name""",
    ).fetchall()

    results = []
    for item in items:
        item_id = item["id"]
        reorder_level = to_decimal(str(item["reorder_level"]))
        reorder_qty = to_decimal(str(item["reorder_qty"])) if item["reorder_qty"] else Decimal("0")

        # Calculate current stock across all warehouses for this company
        # Uses decimal_sum() aggregate with JOIN — kept as raw SQL
        stock_row = conn.execute(
            """SELECT COALESCE(decimal_sum(sle.actual_qty), '0') AS total_qty
               FROM stock_ledger_entry sle
               JOIN warehouse w ON w.id = sle.warehouse_id
               WHERE sle.item_id = ?
                 AND sle.is_cancelled = 0
                 AND w.company_id = ?""",
            (item_id, company_id),
        ).fetchone()

        current_stock = to_decimal(str(stock_row["total_qty"]))

        if current_stock <= reorder_level:
            shortfall = round_currency(reorder_level - current_stock)
            results.append({
                "item_id": item_id,
                "item_code": item["item_code"],
                "item_name": item["item_name"],
                "current_stock": str(round_currency(current_stock)),
                "reorder_level": str(round_currency(reorder_level)),
                "reorder_qty": str(round_currency(reorder_qty)),
                "shortfall": str(shortfall),
            })

    ok({
        "items_below_reorder": len(results),
        "items": results,
    })


# ---------------------------------------------------------------------------
# import-items
# ---------------------------------------------------------------------------

def import_items(conn, args):
    """Bulk import items from a CSV file.

    CSV columns: item_code, name, uom, group (optional),
    valuation_method (optional), description (optional).

    Items are globally unique by item_code (no company_id on item table).
    """
    csv_path = args.csv_path
    if not csv_path:
        err("--csv-path is required")

    # Path safety: resolve symlinks, require .csv extension, must be a regular file
    csv_real = os.path.realpath(csv_path)
    if not csv_real.lower().endswith(".csv"):
        err("--csv-path must point to a .csv file")
    if not os.path.isfile(csv_real):
        err(f"File not found: {csv_path}")

    from erpclaw_lib.csv_import import validate_csv, parse_csv_rows
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args

    errors = validate_csv(csv_real, "item")
    if errors:
        err(f"CSV validation failed: {'; '.join(errors)}")

    rows = parse_csv_rows(csv_real, "item")
    if not rows:
        err("CSV file is empty")

    imported = 0
    skipped = 0
    for row in rows:
        item_code = row.get("item_code", "")
        name = row.get("name", "")
        uom = row.get("uom", "Nos")

        # Check for duplicate (item_code is globally unique)
        item_t = Table("item")
        dup_q = Q.from_(item_t).select(item_t.id).where(item_t.item_code == P())
        existing = conn.execute(dup_q.get_sql(), (item_code,)).fetchone()
        if existing:
            skipped += 1
            continue

        # Look up or default item group
        group_name = row.get("group")
        group_id = None
        if group_name:
            ig_t = Table("item_group")
            grp_q = Q.from_(ig_t).select(ig_t.id).where(ig_t.name == P())
            grp = conn.execute(grp_q.get_sql(), (group_name,)).fetchone()
            if grp:
                group_id = grp["id"]

        item_id = str(uuid.uuid4())
        ins_t = Table("item")
        ins_q = Q.into(ins_t).columns(
            "id", "item_code", "item_name", "item_group_id",
            "stock_uom", "valuation_method", "description", "status",
        ).insert(P(), P(), P(), P(), P(), P(), P(), "active")
        conn.execute(
            ins_q.get_sql(),
            (item_id, item_code, name, group_id, uom,
             row.get("valuation_method", "moving_average").lower(),
             row.get("description")),
        )
        imported += 1

    conn.commit()
    ok({"imported": imported, "skipped": skipped, "total_rows": len(rows)})


# ---------------------------------------------------------------------------
# Feature #4: get-projected-qty
# ---------------------------------------------------------------------------

def get_projected_qty(conn, args):
    """Calculate projected quantity for an item in a warehouse.

    projected_qty = actual_qty + ordered_qty (pending receipt) - reserved_qty (pending delivery)

    ordered_qty = SUM(po_item.quantity - po_item.received_qty) for open POs
    reserved_qty = SUM(so_item.quantity - so_item.delivered_qty) for confirmed SOs
    """
    if not args.item_id:
        err("--item-id is required")
    if not args.warehouse_id:
        err("--warehouse-id is required")

    # Verify item exists
    item_t = Table("item")
    q = Q.from_(item_t).select(item_t.id, item_t.item_code, item_t.item_name).where(item_t.id == P())
    item = conn.execute(q.get_sql(), (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")

    # Verify warehouse exists
    wh_t = Table("warehouse")
    q = Q.from_(wh_t).select(wh_t.id).where(wh_t.id == P())
    wh = conn.execute(q.get_sql(), (args.warehouse_id,)).fetchone()
    if not wh:
        err(f"Warehouse {args.warehouse_id} not found")

    # 1. Actual qty from SLE
    balance = get_stock_balance(conn, args.item_id, args.warehouse_id)
    actual_qty = to_decimal(balance["qty"])

    # 2. Ordered qty: open PO items not yet fully received
    # PO statuses that indicate pending receipt: confirmed, partially_received
    po_rows = conn.execute(
        """SELECT poi.quantity, poi.received_qty
        FROM purchase_order_item poi
        JOIN purchase_order po ON po.id = poi.purchase_order_id
        WHERE poi.item_id = ?
          AND (poi.warehouse_id = ? OR poi.warehouse_id IS NULL)
          AND po.status IN ('confirmed', 'partially_received')""",
        (args.item_id, args.warehouse_id),
    ).fetchall()
    ordered_qty = round_currency(sum(
        (max(to_decimal(r["quantity"]) - to_decimal(r["received_qty"]), Decimal("0")) for r in po_rows),
        Decimal("0"),
    ))

    # 3. Reserved qty (ADR-0026): read PERSISTED active stock_reservation_entry
    # rows for this (item, warehouse). When no persisted rows exist, FALL BACK to
    # the original SO-derived computation (confirmed SO items not yet delivered),
    # so existing callers see no behavioral change and the return shape is
    # unchanged (reserved_qty stays Decimal-as-text).
    res_rows = conn.execute(
        "SELECT reserved_qty FROM stock_reservation_entry "
        "WHERE item_id = ? AND warehouse_id = ? AND status = 'active'",
        (args.item_id, args.warehouse_id),
    ).fetchall()
    if res_rows:
        reserved_qty = round_currency(sum(
            (to_decimal(r["reserved_qty"]) for r in res_rows), Decimal("0"),
        ))
    else:
        # Fallback: SO statuses indicating pending delivery (confirmed, partially_delivered).
        so_rows = conn.execute(
            """SELECT soi.quantity, soi.delivered_qty
            FROM sales_order_item soi
            JOIN sales_order so_ ON so_.id = soi.sales_order_id
            WHERE soi.item_id = ?
              AND (soi.warehouse_id = ? OR soi.warehouse_id IS NULL)
              AND so_.status IN ('confirmed', 'partially_delivered')""",
            (args.item_id, args.warehouse_id),
        ).fetchall()
        reserved_qty = round_currency(sum(
            (max(to_decimal(r["quantity"]) - to_decimal(r["delivered_qty"]), Decimal("0")) for r in so_rows),
            Decimal("0"),
        ))

    projected_qty = round_currency(actual_qty + ordered_qty - reserved_qty)

    ok({
        "item_id": args.item_id,
        "item_code": item["item_code"],
        "item_name": item["item_name"],
        "warehouse_id": args.warehouse_id,
        "actual_qty": str(round_currency(actual_qty)),
        "ordered_qty": str(ordered_qty),
        "reserved_qty": str(reserved_qty),
        "projected_qty": str(projected_qty),
    })


# ---------------------------------------------------------------------------
# Feature #5: Item Variants
# ---------------------------------------------------------------------------

def add_item_attribute(conn, args):
    """Add an attribute definition to a template item.

    Marks the item as has_variants=1 (template).
    --attribute-values is a JSON array, e.g. '["Red","Blue","Green"]'
    """
    if not args.item_id:
        err("--item-id is required")
    if not args.attribute_name:
        err("--attribute-name is required")
    if not args.attribute_values:
        err("--attribute-values is required (JSON array)")

    # Validate item exists
    item_t = Table("item")
    q = Q.from_(item_t).select(item_t.id, item_t.variant_of).where(item_t.id == P())
    item = conn.execute(q.get_sql(), (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")
    if item["variant_of"]:
        err("Cannot add attributes to a variant item — add to the template instead")

    # Parse values
    values = _parse_json_arg(args.attribute_values, "attribute-values")
    if not isinstance(values, list) or len(values) == 0:
        err("--attribute-values must be a non-empty JSON array")

    # Check for duplicate attribute name on this item
    attr_t = Table("item_attribute")
    q = (Q.from_(attr_t).select(attr_t.id)
         .where(attr_t.item_id == P())
         .where(attr_t.attribute_name == P()))
    existing = conn.execute(q.get_sql(), (args.item_id, args.attribute_name)).fetchone()
    if existing:
        err(f"Attribute '{args.attribute_name}' already exists for this item")

    attr_id = str(uuid.uuid4())
    q = Q.into(attr_t).columns(
        "id", "item_id", "attribute_name", "attribute_values",
    ).insert(P(), P(), P(), P())
    conn.execute(q.get_sql(), (attr_id, args.item_id, args.attribute_name, json.dumps(values)))

    # Mark item as template
    q = (Q.update(item_t)
         .set(item_t.has_variants, 1)
         .set(item_t.updated_at, _NOW)
         .where(item_t.id == P()))
    conn.execute(q.get_sql(), (args.item_id,))

    audit(conn, "erpclaw-inventory", "add-item-attribute", "item_attribute", attr_id,
          new_values={"item_id": args.item_id, "attribute_name": args.attribute_name})
    conn.commit()
    ok({"attribute_id": attr_id, "item_id": args.item_id,
        "attribute_name": args.attribute_name, "values": values})


def create_item_variant(conn, args):
    """Create a single item variant from a template item with specific attribute values.

    --attributes is a JSON object, e.g. '{"Color": "Red", "Size": "Large"}'
    """
    if not args.template_item_id:
        err("--template-item-id is required")
    if not args.attributes:
        err("--attributes is required (JSON object)")

    # Validate template item
    item_t = Table("item")
    q = Q.from_(item_t).select(item_t.star).where(item_t.id == P())
    template = conn.execute(q.get_sql(), (args.template_item_id,)).fetchone()
    if not template:
        err(f"Template item {args.template_item_id} not found")
    if not template["has_variants"]:
        err("Item is not a template (has_variants must be 1)")

    attributes = _parse_json_arg(args.attributes, "attributes")
    if not isinstance(attributes, dict) or len(attributes) == 0:
        err("--attributes must be a non-empty JSON object")

    # Validate attribute values against template's attribute definitions
    attr_t = Table("item_attribute")
    q = Q.from_(attr_t).select(attr_t.attribute_name, attr_t.attribute_values).where(attr_t.item_id == P())
    template_attrs = conn.execute(q.get_sql(), (args.template_item_id,)).fetchall()
    template_attr_map = {}
    for a in template_attrs:
        template_attr_map[a["attribute_name"]] = json.loads(a["attribute_values"]) if a["attribute_values"] else []

    for attr_name, attr_val in attributes.items():
        if attr_name not in template_attr_map:
            err(f"Attribute '{attr_name}' is not defined on the template item")
        if attr_val not in template_attr_map[attr_name]:
            err(f"Value '{attr_val}' is not valid for attribute '{attr_name}'. "
                f"Valid: {template_attr_map[attr_name]}")

    # Build variant code: template_code-Val1-Val2
    suffix = "-".join(str(v) for v in attributes.values())
    variant_code = f"{template['item_code']}-{suffix}"
    variant_name = f"{template['item_name']} ({suffix})"

    # Check for duplicate variant
    dup_q = Q.from_(item_t).select(item_t.id).where(item_t.item_code == P())
    existing = conn.execute(dup_q.get_sql(), (variant_code,)).fetchone()
    if existing:
        err(f"Variant '{variant_code}' already exists")

    variant_id = str(uuid.uuid4())
    q = Q.into(item_t).columns(
        "id", "item_code", "item_name", "item_group_id", "item_type", "stock_uom",
        "valuation_method", "is_stock_item", "is_purchase_item", "is_sales_item",
        "has_batch", "has_serial", "standard_rate", "variant_of", "status",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), "active")
    try:
        conn.execute(q.get_sql(), (
            variant_id, variant_code, variant_name,
            template["item_group_id"], template["item_type"],
            template["stock_uom"], template["valuation_method"],
            template["is_stock_item"], template["is_purchase_item"],
            template["is_sales_item"], template["has_batch"],
            template["has_serial"], template["standard_rate"],
            args.template_item_id,
        ))
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err("Variant creation failed — check for duplicates")

    # Store variant's attribute values as item_attribute rows
    for attr_name, attr_val in attributes.items():
        va_id = str(uuid.uuid4())
        q = Q.into(attr_t).columns(
            "id", "item_id", "attribute_name", "attribute_values",
        ).insert(P(), P(), P(), P())
        conn.execute(q.get_sql(), (va_id, variant_id, attr_name, json.dumps(attr_val)))

    audit(conn, "erpclaw-inventory", "create-item-variant", "item", variant_id,
          new_values={"variant_of": args.template_item_id, "attributes": attributes})
    conn.commit()
    ok({"variant_id": variant_id, "item_code": variant_code,
        "item_name": variant_name, "template_item_id": args.template_item_id,
        "attributes": attributes})


def generate_item_variants(conn, args):
    """Generate all possible variants from a template item's attributes (cartesian product)."""
    if not args.template_item_id:
        err("--template-item-id is required")

    # Validate template
    item_t = Table("item")
    q = Q.from_(item_t).select(item_t.star).where(item_t.id == P())
    template = conn.execute(q.get_sql(), (args.template_item_id,)).fetchone()
    if not template:
        err(f"Template item {args.template_item_id} not found")
    if not template["has_variants"]:
        err("Item is not a template (has_variants must be 1)")

    # Get all attributes
    attr_t = Table("item_attribute")
    q = (Q.from_(attr_t).select(attr_t.attribute_name, attr_t.attribute_values)
         .where(attr_t.item_id == P())
         .orderby(attr_t.attribute_name))
    attrs = conn.execute(q.get_sql(), (args.template_item_id,)).fetchall()
    if not attrs:
        err("Template has no attributes defined — use add-item-attribute first")

    attr_names = []
    attr_value_lists = []
    for a in attrs:
        attr_names.append(a["attribute_name"])
        values = json.loads(a["attribute_values"]) if a["attribute_values"] else []
        if not values:
            err(f"Attribute '{a['attribute_name']}' has no values")
        attr_value_lists.append(values)

    # Cartesian product
    combinations = list(itertools.product(*attr_value_lists))

    created = []
    skipped = []
    for combo in combinations:
        attributes = dict(zip(attr_names, combo))
        suffix = "-".join(str(v) for v in combo)
        variant_code = f"{template['item_code']}-{suffix}"
        variant_name = f"{template['item_name']} ({suffix})"

        # Skip if already exists
        dup_q = Q.from_(item_t).select(item_t.id).where(item_t.item_code == P())
        existing = conn.execute(dup_q.get_sql(), (variant_code,)).fetchone()
        if existing:
            skipped.append(variant_code)
            continue

        variant_id = str(uuid.uuid4())
        q = Q.into(item_t).columns(
            "id", "item_code", "item_name", "item_group_id", "item_type", "stock_uom",
            "valuation_method", "is_stock_item", "is_purchase_item", "is_sales_item",
            "has_batch", "has_serial", "standard_rate", "variant_of", "status",
        ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), "active")
        conn.execute(q.get_sql(), (
            variant_id, variant_code, variant_name,
            template["item_group_id"], template["item_type"],
            template["stock_uom"], template["valuation_method"],
            template["is_stock_item"], template["is_purchase_item"],
            template["is_sales_item"], template["has_batch"],
            template["has_serial"], template["standard_rate"],
            args.template_item_id,
        ))

        # Store variant attribute values
        for attr_name, attr_val in attributes.items():
            va_id = str(uuid.uuid4())
            q = Q.into(attr_t).columns(
                "id", "item_id", "attribute_name", "attribute_values",
            ).insert(P(), P(), P(), P())
            conn.execute(q.get_sql(), (va_id, variant_id, attr_name, json.dumps(attr_val)))

        created.append({"variant_id": variant_id, "item_code": variant_code, "attributes": attributes})

    conn.commit()
    ok({
        "template_item_id": args.template_item_id,
        "created": len(created),
        "skipped": len(skipped),
        "skipped_codes": skipped,
        "variants": created,
    })


def list_item_variants(conn, args):
    """List all variants of a template item."""
    if not args.template_item_id:
        err("--template-item-id is required")

    # Verify template exists
    item_t = Table("item")
    q = Q.from_(item_t).select(item_t.id, item_t.has_variants).where(item_t.id == P())
    template = conn.execute(q.get_sql(), (args.template_item_id,)).fetchone()
    if not template:
        err(f"Template item {args.template_item_id} not found")

    # Get all variants
    q = (Q.from_(item_t)
         .select(item_t.id, item_t.item_code, item_t.item_name, item_t.status, item_t.standard_rate)
         .where(item_t.variant_of == P())
         .orderby(item_t.item_code))
    variants = conn.execute(q.get_sql(), (args.template_item_id,)).fetchall()

    # For each variant, get its attributes
    attr_t = Table("item_attribute")
    results = []
    for v in variants:
        q = (Q.from_(attr_t)
             .select(attr_t.attribute_name, attr_t.attribute_values)
             .where(attr_t.item_id == P()))
        attrs = conn.execute(q.get_sql(), (v["id"],)).fetchall()
        attr_dict = {}
        for a in attrs:
            val = json.loads(a["attribute_values"]) if a["attribute_values"] else None
            attr_dict[a["attribute_name"]] = val
        results.append({
            "variant_id": v["id"],
            "item_code": v["item_code"],
            "item_name": v["item_name"],
            "status": v["status"],
            "standard_rate": v["standard_rate"],
            "attributes": attr_dict,
        })

    ok({
        "template_item_id": args.template_item_id,
        "count": len(results),
        "variants": results,
    })


# ---------------------------------------------------------------------------
# Feature #6: Item Supplier (Min Order Qty)
# ---------------------------------------------------------------------------

def add_item_supplier(conn, args):
    """Link an item to a supplier with min order qty and lead time."""
    if not args.item_id:
        err("--item-id is required")
    if not args.supplier_id:
        err("--supplier-id is required")

    # Validate item
    item_t = Table("item")
    q = Q.from_(item_t).select(item_t.id).where(item_t.id == P())
    item = conn.execute(q.get_sql(), (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")

    # Validate supplier
    sup_t = Table("supplier")
    q = Q.from_(sup_t).select(sup_t.id).where(sup_t.id == P())
    sup = conn.execute(q.get_sql(), (args.supplier_id,)).fetchone()
    if not sup:
        err(f"Supplier {args.supplier_id} not found")

    min_order_qty = str(round_currency(to_decimal(args.min_order_qty or "0")))
    lead_time = int(args.lead_time_days) if args.lead_time_days else None
    priority = int(args.priority) if args.priority is not None else 0

    is_t = Table("item_supplier")
    is_id = str(uuid.uuid4())
    q = Q.into(is_t).columns(
        "id", "item_id", "supplier_id", "min_order_qty", "lead_time_days", "priority",
    ).insert(P(), P(), P(), P(), P(), P())
    try:
        conn.execute(q.get_sql(), (is_id, args.item_id, args.supplier_id, min_order_qty, lead_time, priority))
    except sqlite3.IntegrityError:
        err("This item-supplier link already exists")

    audit(conn, "erpclaw-inventory", "add-item-supplier", "item_supplier", is_id,
          new_values={"item_id": args.item_id, "supplier_id": args.supplier_id,
                      "min_order_qty": min_order_qty})
    conn.commit()
    ok({"item_supplier_id": is_id, "item_id": args.item_id,
        "supplier_id": args.supplier_id, "min_order_qty": min_order_qty,
        "lead_time_days": lead_time, "priority": priority})


def list_item_suppliers(conn, args):
    """List all suppliers for an item, or all items for a supplier."""
    is_t = Table("item_supplier")
    item_t = Table("item")
    sup_t = Table("supplier")

    q = (Q.from_(is_t)
         .left_join(item_t).on(item_t.id == is_t.item_id)
         .left_join(sup_t).on(sup_t.id == is_t.supplier_id)
         .select(
             is_t.id, is_t.item_id, is_t.supplier_id,
             is_t.min_order_qty, is_t.lead_time_days, is_t.priority,
             item_t.item_code, item_t.item_name,
             sup_t.name.as_("supplier_name"),
         )
         .orderby(is_t.priority))

    params = []
    if args.item_id:
        q = q.where(is_t.item_id == P())
        params.append(args.item_id)
    if args.supplier_id:
        q = q.where(is_t.supplier_id == P())
        params.append(args.supplier_id)

    if not args.item_id and not args.supplier_id:
        err("At least one of --item-id or --supplier-id is required")

    rows = conn.execute(q.get_sql(), params).fetchall()
    results = [row_to_dict(r) for r in rows]
    ok({"count": len(results), "item_suppliers": results})


# ===========================================================================
# Wave 2 M5: putaway + pick list + persisted hard reservation (ADR-0026).
# Warehouse-level granularity V1; no bin schema (pick_list_item.source_warehouse_bin
# is a free-TEXT hint only). erpclaw-inventory owns + writes all four tables.
# ===========================================================================

# Reservation lifecycle states (CHECK in stock_reservation_entry.status).
RESERVATION_STATUSES = ("active", "released", "consumed")
# Voucher types a reservation may bind to (CHECK in voucher_type).
RESERVATION_VOUCHER_TYPES = ("sales_order", "pick_list", "manual")
# Pick-list lifecycle states.
PICK_LIST_STATUSES = ("draft", "submitted", "picked", "completed", "cancelled")


def _active_reserved_qty(conn, item_id, warehouse_id, exclude_reservation_id=None):
    """SUM(reserved_qty) over ACTIVE stock_reservation_entry rows for an
    (item, warehouse), as Decimal. Optionally excludes one reservation id (used
    when re-checking headroom while editing/releasing a specific reservation).

    This is the single point of truth for "how much of this warehouse's stock is
    already promised" — read by get-projected-qty (reserved_qty), add-reservation,
    submit-pick-list, and submit-stock-entry's material_issue hard-block.
    """
    sql = ("SELECT reserved_qty FROM stock_reservation_entry "
           "WHERE item_id = ? AND warehouse_id = ? AND status = 'active'")
    params = [item_id, warehouse_id]
    if exclude_reservation_id is not None:
        sql += " AND id != ?"
        params.append(exclude_reservation_id)
    rows = conn.execute(sql, params).fetchall()
    return sum((to_decimal(r["reserved_qty"]) for r in rows), Decimal("0"))


def _available_qty(conn, item_id, warehouse_id, exclude_reservation_id=None):
    """Available-to-reserve / available-to-issue for an (item, warehouse):

        available = actual_qty - SUM(active reservation reserved_qty)

    This is the hard-reservation invariant of ADR-0026: a consumption can never
    drive available below the sum of active reservations. actual_qty comes from
    the SLE balance (the same source get-stock-balance uses).
    """
    balance = get_stock_balance(conn, item_id, warehouse_id)
    actual = to_decimal(balance["qty"])
    reserved = _active_reserved_qty(conn, item_id, warehouse_id, exclude_reservation_id)
    return round_currency(actual - reserved)


def _next_pick_list_name(conn):
    """Generate the next PICK-YYYY-NNNN name from the pick_list table itself.

    Self-contained (owning-module table only): counts existing pick_list rows for
    the current year by the PICK-YYYY- prefix and increments. Zero-padded to 4.
    """
    year = datetime.now(timezone.utc).year
    prefix = f"PICK-{year}-"
    row = conn.execute(
        "SELECT name FROM pick_list WHERE name LIKE ? ORDER BY name DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    if row and row["name"]:
        try:
            seq = int(row["name"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


# --- Putaway rules ---------------------------------------------------------

def add_putaway_rule(conn, args):
    """Add a warehouse-routing rule. At least one of --match-item / --match-item-group."""
    if not args.name:
        err("--name is required")
    if not args.target_warehouse_id:
        err("--target-warehouse is required")
    if not args.match_item_id and not args.match_item_group:
        err("At least one of --match-item or --match-item-group is required")

    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    wh_t = Table("warehouse")
    wh = conn.execute(Q.from_(wh_t).select(wh_t.id).where(wh_t.id == P()).get_sql(),
                      (args.target_warehouse_id,)).fetchone()
    if not wh:
        err(f"Target warehouse {args.target_warehouse_id} not found")

    if args.match_item_id:
        item_t = Table("item")
        it = conn.execute(Q.from_(item_t).select(item_t.id).where(item_t.id == P()).get_sql(),
                          (args.match_item_id,)).fetchone()
        if not it:
            err(f"Match item {args.match_item_id} not found")

    priority = args.priority if args.priority is not None else 100
    rule_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO putaway_rule (id, name, priority, match_item_id, match_item_group, "
        "target_warehouse_id, company_id, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (rule_id, args.name, priority, args.match_item_id, args.match_item_group,
         args.target_warehouse_id, company_id),
    )
    audit(conn, "erpclaw-inventory", "add-putaway-rule", "putaway_rule", rule_id,
          new_values={"name": args.name, "priority": priority,
                      "target_warehouse_id": args.target_warehouse_id})
    conn.commit()
    ok({"putaway_rule_id": rule_id, "name": args.name, "priority": priority})


def list_putaway_rules(conn, args):
    """List putaway rules (optionally active-only), in match precedence order."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    sql = "SELECT * FROM putaway_rule WHERE company_id = ?"
    params = [company_id]
    if getattr(args, 'active_only', False):
        sql += " AND is_active = 1"
    # Match precedence: item match before item_group, then priority ASC.
    sql += (" ORDER BY CASE WHEN match_item_id IS NOT NULL THEN 0 ELSE 1 END, "
            "priority ASC, created_at ASC")
    rows = conn.execute(sql, params).fetchall()
    results = [row_to_dict(r) for r in rows]
    ok({"count": len(results), "putaway_rules": results})


def update_putaway_rule(conn, args):
    """Update a putaway rule's mutable fields."""
    if not args.id:
        err("--id is required")
    rule = conn.execute("SELECT * FROM putaway_rule WHERE id = ?", (args.id,)).fetchone()
    if not rule:
        err(f"Putaway rule {args.id} not found")

    data, updated = {}, []
    if args.name is not None:
        data["name"] = args.name; updated.append("name")
    if args.priority is not None:
        data["priority"] = args.priority; updated.append("priority")
    if args.target_warehouse_id is not None:
        wh_t = Table("warehouse")
        if not conn.execute(Q.from_(wh_t).select(wh_t.id).where(wh_t.id == P()).get_sql(),
                            (args.target_warehouse_id,)).fetchone():
            err(f"Target warehouse {args.target_warehouse_id} not found")
        data["target_warehouse_id"] = args.target_warehouse_id; updated.append("target_warehouse_id")
    if args.match_item_group is not None:
        data["match_item_group"] = args.match_item_group; updated.append("match_item_group")
    if not updated:
        err("No fields to update")
    data["updated_at"] = now()
    sql, params = dynamic_update("putaway_rule", data, where={"id": args.id})
    conn.execute(sql, params)
    audit(conn, "erpclaw-inventory", "update-putaway-rule", "putaway_rule", args.id,
          new_values={"updated_fields": updated})
    conn.commit()
    ok({"putaway_rule_id": args.id, "updated_fields": updated})


def delete_putaway_rule(conn, args):
    """Soft-delete a putaway rule (is_active=0)."""
    if not args.id:
        err("--id is required")
    rule = conn.execute("SELECT * FROM putaway_rule WHERE id = ?", (args.id,)).fetchone()
    if not rule:
        err(f"Putaway rule {args.id} not found")
    conn.execute(
        "UPDATE putaway_rule SET is_active = 0, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (args.id,),
    )
    audit(conn, "erpclaw-inventory", "delete-putaway-rule", "putaway_rule", args.id,
          new_values={"is_active": 0})
    conn.commit()
    ok({"putaway_rule_id": args.id, "deactivated": True})


def _resolve_putaway_target(conn, company_id, item_id, item_group_value):
    """Return the target_warehouse_id of the highest-precedence active rule that
    matches an item, or None. Precedence: item match > item_group match; within a
    class, priority ASC (deterministic — same input always routes the same way)."""
    # 1. Item-level match (highest precedence).
    row = conn.execute(
        "SELECT target_warehouse_id FROM putaway_rule "
        "WHERE company_id = ? AND is_active = 1 AND match_item_id = ? "
        "ORDER BY priority ASC, created_at ASC LIMIT 1",
        (company_id, item_id),
    ).fetchone()
    if row:
        return row["target_warehouse_id"]
    # 2. Item-group match (fallback).
    if item_group_value:
        row = conn.execute(
            "SELECT target_warehouse_id FROM putaway_rule "
            "WHERE company_id = ? AND is_active = 1 AND match_item_group = ? "
            "ORDER BY priority ASC, created_at ASC LIMIT 1",
            (company_id, item_group_value),
        ).fetchone()
        if row:
            return row["target_warehouse_id"]
    return None


def apply_putaway_on_receipt(conn, args):
    """Compute putaway routing for a submitted material_receipt stock entry.

    Deterministic: returns, per received line, the target warehouse the active
    putaway rules route it to (item match > item_group match, then priority ASC).
    A standalone read/plan helper — it does NOT mutate the SLE here; callers use
    the routing to drive a follow-on cross-warehouse material_transfer. Same rules
    + same input = same routing.
    """
    if not args.stock_entry_id:
        err("--stock-entry SE is required")
    se = conn.execute("SELECT * FROM stock_entry WHERE id = ?", (args.stock_entry_id,)).fetchone()
    if not se:
        err(f"Stock entry {args.stock_entry_id} not found")
    se_dict = row_to_dict(se)
    if se_dict["stock_entry_type"] != "material_receipt":
        err(f"Putaway applies to material_receipt only (entry is '{se_dict['stock_entry_type']}')")
    company_id = se_dict["company_id"]

    items = conn.execute(
        "SELECT * FROM stock_entry_item WHERE stock_entry_id = ? ORDER BY id",
        (args.stock_entry_id,),
    ).fetchall()

    routes = []
    for row in items:
        line = row_to_dict(row)
        item_id = line["item_id"]
        ig = conn.execute(
            "SELECT ig.name AS gname FROM item i "
            "LEFT JOIN item_group ig ON ig.id = i.item_group_id WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        item_group_value = ig["gname"] if ig else None
        target = _resolve_putaway_target(conn, company_id, item_id, item_group_value)
        routes.append({
            "item_id": item_id,
            "received_warehouse_id": line.get("to_warehouse_id"),
            "target_warehouse_id": target,
            "needs_transfer": bool(target and target != line.get("to_warehouse_id")),
            "qty": str(round_currency(to_decimal(line["quantity"]))),
        })
    ok({"stock_entry_id": args.stock_entry_id, "routes": routes,
        "routed_count": sum(1 for r in routes if r["target_warehouse_id"])})


# --- Pick lists ------------------------------------------------------------

def create_pick_list(conn, args):
    """Create a draft pick list + items from open lines of a sales order."""
    if not args.sales_order_id:
        err("--from-sales-order SO is required")
    so = conn.execute("SELECT * FROM sales_order WHERE id = ?", (args.sales_order_id,)).fetchone()
    if not so:
        err(f"Sales order {args.sales_order_id} not found")
    so_dict = row_to_dict(so)
    if so_dict["status"] not in ("confirmed", "partially_delivered"):
        err(f"Cannot create pick list: sales order is '{so_dict['status']}' "
            f"(must be 'confirmed' or 'partially_delivered')")

    so_items = conn.execute(
        "SELECT * FROM sales_order_item WHERE sales_order_id = ? ORDER BY id",
        (args.sales_order_id,),
    ).fetchall()
    # Open lines = quantity - delivered_qty > 0.
    open_lines = []
    for row in so_items:
        soi = row_to_dict(row)
        remaining = to_decimal(soi["quantity"]) - to_decimal(soi["delivered_qty"])
        if remaining > 0:
            open_lines.append((soi, remaining))
    if not open_lines:
        err("Sales order has no open lines to pick")

    # from_warehouse: first SO line warehouse, else company default warehouse.
    from_wh = next((soi["warehouse_id"] for soi, _ in open_lines if soi.get("warehouse_id")), None)
    if not from_wh and args.warehouse_id:
        from_wh = args.warehouse_id
    if not from_wh:
        err("No source warehouse on the sales order lines; pass --warehouse-id")

    pick_id = str(uuid.uuid4())
    pick_name = _next_pick_list_name(conn)
    conn.execute(
        "INSERT INTO pick_list (id, name, sales_order_id, from_warehouse_id, status, company_id) "
        "VALUES (?, ?, ?, ?, 'draft', ?)",
        (pick_id, pick_name, args.sales_order_id, from_wh, so_dict["company_id"]),
    )
    line_ids = []
    for soi, remaining in open_lines:
        li_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO pick_list_item (id, pick_list_id, item_id, expected_qty, picked_qty) "
            "VALUES (?, ?, ?, ?, '0')",
            (li_id, pick_id, soi["item_id"], str(round_currency(remaining))),
        )
        line_ids.append(li_id)

    audit(conn, "erpclaw-inventory", "create-pick-list", "pick_list", pick_id,
          new_values={"name": pick_name, "sales_order_id": args.sales_order_id,
                      "line_count": len(line_ids)})
    conn.commit()
    ok({"pick_list_id": pick_id, "name": pick_name, "from_warehouse_id": from_wh,
        "line_count": len(line_ids)})


def add_pick_list_item(conn, args):
    """Add a line to a draft pick list."""
    if not args.pick_list_id:
        err("--pick-list P is required")
    if not args.item_id:
        err("--item I is required")
    if not args.qty:
        err("--qty Q is required")
    pl = conn.execute("SELECT * FROM pick_list WHERE id = ?", (args.pick_list_id,)).fetchone()
    if not pl:
        err(f"Pick list {args.pick_list_id} not found")
    if pl["status"] != "draft":
        err(f"Cannot add line: pick list is '{pl['status']}' (must be 'draft')")
    item = conn.execute("SELECT id FROM item WHERE id = ?", (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")
    try:
        qty = to_decimal(args.qty)
    except (InvalidOperation, ValueError):
        err(f"--qty must be a number: {args.qty}")
    if qty <= 0:
        err("--qty must be > 0")

    li_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO pick_list_item (id, pick_list_id, item_id, expected_qty, picked_qty, source_warehouse_bin) "
        "VALUES (?, ?, ?, ?, '0', ?)",
        (li_id, args.pick_list_id, args.item_id, str(round_currency(qty)), args.source_bin),
    )
    audit(conn, "erpclaw-inventory", "add-pick-list-item", "pick_list_item", li_id,
          new_values={"pick_list_id": args.pick_list_id, "item_id": args.item_id})
    conn.commit()
    ok({"pick_list_item_id": li_id, "pick_list_id": args.pick_list_id,
        "item_id": args.item_id, "expected_qty": str(round_currency(qty))})


def submit_pick_list(conn, args):
    """Submit a draft pick list: validate availability + create active reservations.

    Blocked if any line's expected_qty exceeds available qty (actual - active
    reservations) at the source warehouse. Single transaction: every reservation
    is written or none. Status draft -> submitted.
    """
    if not args.id:
        err("--id is required")
    pl = conn.execute("SELECT * FROM pick_list WHERE id = ?", (args.id,)).fetchone()
    if not pl:
        err(f"Pick list {args.id} not found")
    pl_dict = row_to_dict(pl)
    if pl_dict["status"] != "draft":
        err(f"Cannot submit: pick list is '{pl_dict['status']}' (must be 'draft')")

    lines = conn.execute(
        "SELECT * FROM pick_list_item WHERE pick_list_id = ?", (args.id,)
    ).fetchall()
    if not lines:
        err("Pick list has no items")

    from_wh = pl_dict["from_warehouse_id"]
    # Aggregate expected qty per item (a list may repeat an item across lines).
    per_item = {}
    for row in lines:
        li = row_to_dict(row)
        per_item[li["item_id"]] = per_item.get(li["item_id"], Decimal("0")) + to_decimal(li["expected_qty"])

    # Hard pre-check: every item's total expected must fit in available qty.
    for item_id, need in per_item.items():
        available = _available_qty(conn, item_id, from_wh)
        if need > available:
            err(f"Cannot submit pick list: item {item_id} needs {need} but only "
                f"{available} available at warehouse {from_wh} (actual minus active reservations).")

    # All lines fit — create one active reservation per item (single transaction).
    res_ids = []
    for item_id, need in per_item.items():
        res_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO stock_reservation_entry (id, voucher_type, voucher_id, item_id, "
            "warehouse_id, reserved_qty, status, company_id) "
            "VALUES (?, 'pick_list', ?, ?, ?, ?, 'active', ?)",
            (res_id, args.id, item_id, from_wh, str(round_currency(need)), pl_dict["company_id"]),
        )
        res_ids.append(res_id)

    conn.execute(
        "UPDATE pick_list SET status = 'submitted', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (args.id,),
    )
    audit(conn, "erpclaw-inventory", "submit-pick-list", "pick_list", args.id,
          new_values={"status": "submitted", "reservations_created": len(res_ids)})
    conn.commit()
    ok({"pick_list_id": args.id, "pick_list_status": "submitted",
        "reservations_created": len(res_ids)})


def mark_picked(conn, args):
    """Record actual picked qty on a pick list line. When every line is fully
    picked (picked_qty == expected_qty), the pick list flips to 'picked'."""
    if not args.pick_list_id:
        err("--pick-list P is required")
    if not args.item_id:
        err("--item I is required")
    if args.picked_qty is None:
        err("--picked-qty Q is required")
    pl = conn.execute("SELECT * FROM pick_list WHERE id = ?", (args.pick_list_id,)).fetchone()
    if not pl:
        err(f"Pick list {args.pick_list_id} not found")
    if pl["status"] not in ("submitted", "picked"):
        err(f"Cannot mark picked: pick list is '{pl['status']}' (must be 'submitted' or 'picked')")
    line = conn.execute(
        "SELECT * FROM pick_list_item WHERE pick_list_id = ? AND item_id = ? LIMIT 1",
        (args.pick_list_id, args.item_id),
    ).fetchone()
    if not line:
        err(f"Item {args.item_id} not on pick list {args.pick_list_id}")
    try:
        picked = to_decimal(args.picked_qty)
    except (InvalidOperation, ValueError):
        err(f"--picked-qty must be a number: {args.picked_qty}")
    if picked < 0:
        err("--picked-qty must be >= 0")
    expected = to_decimal(line["expected_qty"])
    if picked > expected:
        err(f"--picked-qty {picked} exceeds expected {expected}")

    conn.execute(
        "UPDATE pick_list_item SET picked_qty = ? WHERE id = ?",
        (str(round_currency(picked)), line["id"]),
    )

    # Full pick = every line picked_qty == expected_qty (and at least one line).
    # Compared as Decimal in Python (TEXT-stored money sorts lexically, not
    # numerically, so an SQL string comparison would be wrong).
    all_lines = conn.execute(
        "SELECT expected_qty, picked_qty FROM pick_list_item WHERE pick_list_id = ?",
        (args.pick_list_id,),
    ).fetchall()
    fully_picked = bool(all_lines) and all(
        to_decimal(r["picked_qty"]) == to_decimal(r["expected_qty"]) for r in all_lines
    )
    if fully_picked:
        conn.execute(
            "UPDATE pick_list SET status = 'picked', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
            (args.pick_list_id,),
        )
    audit(conn, "erpclaw-inventory", "mark-picked", "pick_list_item", line["id"],
          new_values={"picked_qty": str(round_currency(picked)), "fully_picked": fully_picked})
    conn.commit()
    ok({"pick_list_id": args.pick_list_id, "item_id": args.item_id,
        "picked_qty": str(round_currency(picked)), "fully_picked": fully_picked,
        "pick_list_status": "picked" if fully_picked else pl["status"]})


def complete_pick_list(conn, args):
    """Complete a picked pick list: flip its reservations to 'consumed', mark the
    list 'completed', and (if SO-linked) generate a delivery note via the selling
    skill (cross-skill subprocess — selling owns delivery_note)."""
    if not args.id:
        err("--id is required")
    pl = conn.execute("SELECT * FROM pick_list WHERE id = ?", (args.id,)).fetchone()
    if not pl:
        err(f"Pick list {args.id} not found")
    pl_dict = row_to_dict(pl)
    if pl_dict["status"] != "picked":
        err(f"Cannot complete: pick list is '{pl_dict['status']}' (must be 'picked')")

    # Flip this pick list's active reservations to consumed.
    consumed = conn.execute(
        "UPDATE stock_reservation_entry SET status = 'consumed', "
        "consumed_at = CAST(CURRENT_TIMESTAMP AS TEXT) "
        "WHERE voucher_type = 'pick_list' AND voucher_id = ? AND status = 'active'",
        (args.id,),
    ).rowcount

    conn.execute(
        "UPDATE pick_list SET status = 'completed', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (args.id,),
    )
    audit(conn, "erpclaw-inventory", "complete-pick-list", "pick_list", args.id,
          new_values={"status": "completed", "reservations_consumed": consumed})
    conn.commit()

    # Cross-skill: generate a delivery note from the SO (selling owns the table).
    # The subprocess must hit the SAME DB this connection operates on, so resolve
    # the path from --db-path or the ERPCLAW_DB_PATH the gateway/tests set.
    delivery_note_id = None
    if pl_dict.get("sales_order_id"):
        target_db = getattr(args, "db_path", None) or os.environ.get("ERPCLAW_DB_PATH")
        delivery_note_id = _create_delivery_note_via_selling(
            pl_dict["sales_order_id"], target_db)

    ok({"pick_list_id": args.id, "pick_list_status": "completed",
        "reservations_consumed": consumed, "delivery_note_id": delivery_note_id})


def _create_delivery_note_via_selling(sales_order_id, db_path):
    """Invoke selling's create-delivery-note via subprocess (selling owns
    delivery_note). Best-effort: returns the new DN id or None. Mirrors the
    established cross-skill subprocess pattern (erpclaw-billing run-billing)."""
    import subprocess
    selling = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "erpclaw-selling", "db_query.py")
    if not os.path.isfile(selling):
        return None
    cmd = [sys.executable, selling, "--action", "create-delivery-note",
           "--sales-order-id", sales_order_id]
    if db_path:
        cmd += ["--db-path", db_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            out = json.loads(proc.stdout)
            if out.get("status") == "ok":
                dn = out.get("delivery_note")
                if isinstance(dn, dict):
                    return dn.get("id")
                return out.get("delivery_note_id")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        pass
    return None


def cancel_pick_list(conn, args):
    """Cancel a pick list: release its active reservations (active -> released)
    and set status 'cancelled'. Reservations are freed, not consumed."""
    if not args.id:
        err("--id is required")
    pl = conn.execute("SELECT * FROM pick_list WHERE id = ?", (args.id,)).fetchone()
    if not pl:
        err(f"Pick list {args.id} not found")
    if pl["status"] in ("completed", "cancelled"):
        err(f"Cannot cancel: pick list is '{pl['status']}'")

    released = conn.execute(
        "UPDATE stock_reservation_entry SET status = 'released', "
        "released_at = CAST(CURRENT_TIMESTAMP AS TEXT) "
        "WHERE voucher_type = 'pick_list' AND voucher_id = ? AND status = 'active'",
        (args.id,),
    ).rowcount
    conn.execute(
        "UPDATE pick_list SET status = 'cancelled', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (args.id,),
    )
    audit(conn, "erpclaw-inventory", "cancel-pick-list", "pick_list", args.id,
          new_values={"status": "cancelled", "reservations_released": released,
                      "reason": args.reason})
    conn.commit()
    ok({"pick_list_id": args.id, "pick_list_status": "cancelled",
        "reservations_released": released})


# --- Manual reservations ---------------------------------------------------

def add_reservation(conn, args):
    """Create a manual active reservation. Blocked if qty > available."""
    if not args.voucher_type:
        err(f"--voucher-type is required (one of: {', '.join(RESERVATION_VOUCHER_TYPES)})")
    if args.voucher_type not in RESERVATION_VOUCHER_TYPES:
        err(f"--voucher-type must be one of: {', '.join(RESERVATION_VOUCHER_TYPES)}")
    if not args.item_id:
        err("--item I is required")
    # --warehouse W (plan) maps to the repack --warehouse flag (dest 'warehouse');
    # also accept --warehouse-id.
    warehouse_id = args.warehouse_id or getattr(args, 'warehouse', None)
    if not warehouse_id:
        err("--warehouse W is required")
    if not args.qty:
        err("--qty Q is required")

    item = conn.execute("SELECT id FROM item WHERE id = ?", (args.item_id,)).fetchone()
    if not item:
        err(f"Item {args.item_id} not found")
    wh = conn.execute("SELECT id, company_id FROM warehouse WHERE id = ?", (warehouse_id,)).fetchone()
    if not wh:
        err(f"Warehouse {warehouse_id} not found")
    try:
        qty = to_decimal(args.qty)
    except (InvalidOperation, ValueError):
        err(f"--qty must be a number: {args.qty}")
    if qty <= 0:
        err("--qty must be > 0")
    # 'manual' may omit a voucher_id; sales_order / pick_list must supply one.
    voucher_id = args.voucher_id
    if args.voucher_type != "manual" and not voucher_id:
        err(f"--voucher-id is required for voucher-type '{args.voucher_type}'")

    available = _available_qty(conn, args.item_id, warehouse_id)
    if qty > available:
        err(f"Cannot reserve {qty}: only {available} available at warehouse "
            f"{warehouse_id} (actual minus active reservations).")

    company_id = wh["company_id"]
    res_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO stock_reservation_entry (id, voucher_type, voucher_id, item_id, "
        "warehouse_id, reserved_qty, status, company_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
        (res_id, args.voucher_type, voucher_id, args.item_id, warehouse_id,
         str(round_currency(qty)), company_id),
    )
    audit(conn, "erpclaw-inventory", "add-reservation", "stock_reservation_entry", res_id,
          new_values={"item_id": args.item_id, "warehouse_id": warehouse_id,
                      "reserved_qty": str(round_currency(qty))})
    conn.commit()
    ok({"reservation_id": res_id, "item_id": args.item_id,
        "warehouse_id": warehouse_id, "reserved_qty": str(round_currency(qty)),
        "reservation_status": "active"})


def release_reservation(conn, args):
    """Release an active reservation (active -> released). Blocked if not active."""
    if not args.id:
        err("--id is required")
    res = conn.execute("SELECT * FROM stock_reservation_entry WHERE id = ?", (args.id,)).fetchone()
    if not res:
        err(f"Reservation {args.id} not found")
    if res["status"] != "active":
        err(f"Cannot release: reservation is '{res['status']}' (must be 'active')")
    conn.execute(
        "UPDATE stock_reservation_entry SET status = 'released', "
        "released_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
        (args.id,),
    )
    audit(conn, "erpclaw-inventory", "release-reservation", "stock_reservation_entry", args.id,
          new_values={"status": "released", "reason": args.reason})
    conn.commit()
    ok({"reservation_id": args.id, "reservation_status": "released"})


def list_reservations(conn, args):
    """List reservations, optionally filtered by item / warehouse / status."""
    sql = "SELECT * FROM stock_reservation_entry WHERE 1=1"
    params = []
    if args.item_id:
        sql += " AND item_id = ?"; params.append(args.item_id)
    warehouse_id = args.warehouse_id or getattr(args, 'warehouse', None)
    if warehouse_id:
        sql += " AND warehouse_id = ?"; params.append(warehouse_id)
    # --status is overloaded (item status) at the parser level; accept the
    # canonical --reservation-status, else a --status value if it names a
    # reservation state.
    status = getattr(args, 'reservation_status', None) or getattr(args, 'item_status', None)
    if status:
        if status not in RESERVATION_STATUSES:
            err(f"--status must be one of: {', '.join(RESERVATION_STATUSES)}")
        sql += " AND status = ?"; params.append(status)
    sql += " ORDER BY reserved_at DESC"
    rows = conn.execute(sql, params).fetchall()
    results = [row_to_dict(r) for r in rows]
    ok({"count": len(results), "reservations": results})


# --- Item alternatives (S7, item-global) -----------------------------------

def add_item_alternative(conn, args):
    """Add a directional item-global substitute relationship.

    Required: --item I, --alternative A.
    Optional: --priority P (default 100; lower = preferred), --conversion-factor C
    (Decimal, default '1'), --notes "...".

    Rejects self-reference (item == alternative) and a duplicate (item, alternative)
    pair. (a,b) and (b,a) are BOTH valid distinct rows — the relationship is
    directional, so adding the reverse is not a duplicate.
    """
    item_id = args.item_id
    alternative_id = getattr(args, 'alternative_item_id', None)
    if not item_id:
        err("--item is required")
    if not alternative_id:
        err("--alternative is required")
    if item_id == alternative_id:
        err("An item cannot be its own alternative (--item and --alternative must differ)")

    item_t = Table("item")
    if not conn.execute(Q.from_(item_t).select(item_t.id).where(item_t.id == P()).get_sql(),
                        (item_id,)).fetchone():
        err(f"Item {item_id} not found")
    if not conn.execute(Q.from_(item_t).select(item_t.id).where(item_t.id == P()).get_sql(),
                        (alternative_id,)).fetchone():
        err(f"Alternative item {alternative_id} not found")

    # Duplicate (item, alternative) pair blocked. The reverse pair (b,a) is a
    # distinct, allowed row — only the exact same direction is rejected.
    dup = conn.execute(
        "SELECT id FROM item_alternative WHERE item_id = ? AND alternative_item_id = ?",
        (item_id, alternative_id),
    ).fetchone()
    if dup:
        err(f"Alternative {alternative_id} already exists for item {item_id}")

    conversion_factor = to_decimal(args.conversion_factor or "1")
    if conversion_factor <= 0:
        err("--conversion-factor must be greater than 0")
    priority = args.priority if args.priority is not None else 100

    alt_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO item_alternative (id, item_id, alternative_item_id, priority, "
        "conversion_factor, notes, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (alt_id, item_id, alternative_id, priority,
         str(round_currency(conversion_factor)), args.notes),
    )
    audit(conn, "erpclaw-inventory", "add-item-alternative", "item_alternative", alt_id,
          new_values={"item_id": item_id, "alternative_item_id": alternative_id,
                      "priority": priority})
    conn.commit()
    ok({"item_alternative_id": alt_id, "item_id": item_id,
        "alternative_item_id": alternative_id, "priority": priority,
        "conversion_factor": str(round_currency(conversion_factor))})


def list_item_alternatives(conn, args):
    """List item alternatives, optionally filtered by --item (the primary item),
    active-only by default. Ordered by priority ASC (preferred first)."""
    sql = ("SELECT ia.*, i.item_code AS alternative_item_code, "
           "i.item_name AS alternative_item_name "
           "FROM item_alternative ia "
           "LEFT JOIN item i ON i.id = ia.alternative_item_id WHERE 1=1")
    params = []
    if args.item_id:
        sql += " AND ia.item_id = ?"; params.append(args.item_id)
    if getattr(args, 'active_only', False):
        sql += " AND ia.is_active = 1"
    sql += " ORDER BY ia.item_id, ia.priority ASC, ia.created_at ASC"
    rows = conn.execute(sql, params).fetchall()
    results = [row_to_dict(r) for r in rows]
    ok({"count": len(results), "item_alternatives": results})


def get_best_alternative_for_item(conn, args):
    """Return the highest-priority active alternative for --item that has enough
    stock to cover --required-qty at --warehouse.

    Ranking: priority ASC (lower = preferred); ties broken by available qty at the
    warehouse (more available wins). Required qty is measured against the
    alternative's own available stock, adjusted by its conversion_factor:
    a substitute with conversion_factor 2 needs 2x the units to replace 1 unit of
    the primary, so the qty of substitute needed = required_qty * conversion_factor.

    With no --warehouse, stock is not checked — the highest-priority active
    alternative is returned. No match (none in stock / none defined) is a clean
    exit-0 result, not an error.
    """
    item_id = args.item_id
    if not item_id:
        err("--item is required")
    item_t = Table("item")
    if not conn.execute(Q.from_(item_t).select(item_t.id).where(item_t.id == P()).get_sql(),
                        (item_id,)).fetchone():
        err(f"Item {item_id} not found")

    required_qty = to_decimal(args.qty) if getattr(args, 'qty', None) else Decimal("0")
    if required_qty < 0:
        err("--required-qty cannot be negative")
    warehouse_id = args.warehouse_id or getattr(args, 'warehouse', None)
    if warehouse_id:
        wh_t = Table("warehouse")
        if not conn.execute(Q.from_(wh_t).select(wh_t.id).where(wh_t.id == P()).get_sql(),
                            (warehouse_id,)).fetchone():
            err(f"Warehouse {warehouse_id} not found")

    # Active candidates, ordered by priority ASC (created_at as a deterministic
    # secondary key). The available-qty tie-break is applied in Python because it
    # needs the SLE-balance read (not a column).
    rows = conn.execute(
        "SELECT ia.*, i.item_code AS alternative_item_code, "
        "i.item_name AS alternative_item_name "
        "FROM item_alternative ia "
        "LEFT JOIN item i ON i.id = ia.alternative_item_id "
        "WHERE ia.item_id = ? AND ia.is_active = 1 "
        "ORDER BY ia.priority ASC, ia.created_at ASC",
        (item_id,),
    ).fetchall()

    candidates = []
    for r in rows:
        d = row_to_dict(r)
        conv = to_decimal(d.get("conversion_factor") or "1")
        if warehouse_id:
            available = _available_qty(conn, d["alternative_item_id"], warehouse_id)
            needed = round_currency(required_qty * conv)
            if available < needed:
                continue  # not enough stock to substitute
            d["available_qty"] = str(available)
        else:
            d["available_qty"] = None
        d["required_substitute_qty"] = str(round_currency(required_qty * conv))
        candidates.append((d, to_decimal(str(d.get("priority"))),
                           _available_qty(conn, d["alternative_item_id"], warehouse_id)
                           if warehouse_id else Decimal("0")))

    if not candidates:
        ok({"item_id": item_id, "warehouse_id": warehouse_id,
            "required_qty": str(round_currency(required_qty)),
            "best_alternative": None,
            "message": "No active alternative with sufficient stock"})
        return

    # priority ASC (already sorted), tie-break by available qty DESC.
    candidates.sort(key=lambda c: (c[1], -c[2]))
    best = candidates[0][0]
    ok({"item_id": item_id, "warehouse_id": warehouse_id,
        "required_qty": str(round_currency(required_qty)),
        "best_alternative": best})


def remove_item_alternative(conn, args):
    """Soft-delete an item alternative (is_active=0). Idempotent on an already
    inactive row; errors only on an unknown id."""
    if not args.id:
        err("--id is required")
    row = conn.execute("SELECT * FROM item_alternative WHERE id = ?", (args.id,)).fetchone()
    if not row:
        err(f"Item alternative {args.id} not found")
    conn.execute(
        "UPDATE item_alternative SET is_active = 0 WHERE id = ?",
        (args.id,),
    )
    audit(conn, "erpclaw-inventory", "remove-item-alternative", "item_alternative", args.id,
          new_values={"is_active": 0})
    conn.commit()
    ok({"item_alternative_id": args.id, "deactivated": True})


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

ACTIONS = {
    "add-item": add_item,
    "update-item": update_item,
    "get-item": get_item,
    "list-items": list_items,
    "resolve-item": resolve_item,
    "add-item-group": add_item_group,
    "list-item-groups": list_item_groups,
    "add-warehouse": add_warehouse,
    "update-warehouse": update_warehouse,
    "list-warehouses": list_warehouses,
    "add-stock-entry": add_stock_entry,
    "add-repack-stock-entry": add_repack_stock_entry,
    "add-material-consumption": add_material_consumption,
    "get-stock-entry": get_stock_entry,
    "list-stock-entries": list_stock_entries,
    "submit-stock-entry": submit_stock_entry,
    "cancel-stock-entry": cancel_stock_entry,
    "create-stock-ledger-entries": create_stock_ledger_entries,
    "reverse-stock-ledger-entries": reverse_stock_ledger_entries,
    "get-stock-balance": get_stock_balance_action,
    "stock-balance": stock_balance_report,  # alias — "stock balance" routes to company-wide report
    "stock-balance-report": stock_balance_report,
    "stock-ledger-report": stock_ledger_report,
    "add-batch": add_batch,
    "list-batches": list_batches,
    "add-serial-number": add_serial_number,
    "list-serial-numbers": list_serial_numbers,
    "add-price-list": add_price_list,
    "add-item-price": add_item_price,
    "get-item-price": get_item_price,
    "add-pricing-rule": add_pricing_rule,
    "add-stock-reconciliation": add_stock_reconciliation,
    "submit-stock-reconciliation": submit_stock_reconciliation,
    "revalue-stock": revalue_stock,
    "list-stock-revaluations": list_stock_revaluations,
    "get-stock-revaluation": get_stock_revaluation,
    "cancel-stock-revaluation": cancel_stock_revaluation,
    "check-reorder": check_reorder,
    "import-items": import_items,
    "get-projected-qty": get_projected_qty,
    "add-item-attribute": add_item_attribute,
    "create-item-variant": create_item_variant,
    "generate-item-variants": generate_item_variants,
    "list-item-variants": list_item_variants,
    "add-item-supplier": add_item_supplier,
    "list-item-suppliers": list_item_suppliers,
    "status": status_action,
    # Wave 2 M5: putaway + pick list + persisted hard reservation (ADR-0026).
    "add-putaway-rule": add_putaway_rule,
    "list-putaway-rules": list_putaway_rules,
    "update-putaway-rule": update_putaway_rule,
    "delete-putaway-rule": delete_putaway_rule,
    "apply-putaway-on-receipt": apply_putaway_on_receipt,
    "create-pick-list": create_pick_list,
    "add-pick-list-item": add_pick_list_item,
    "submit-pick-list": submit_pick_list,
    "mark-picked": mark_picked,
    "complete-pick-list": complete_pick_list,
    "cancel-pick-list": cancel_pick_list,
    "add-reservation": add_reservation,
    "release-reservation": release_reservation,
    "list-reservations": list_reservations,
    # Wave 2 S7: item-global alternatives / substitutes.
    "add-item-alternative": add_item_alternative,
    "list-item-alternatives": list_item_alternatives,
    "get-best-alternative-for-item": get_best_alternative_for_item,
    "remove-item-alternative": remove_item_alternative,
}


def main():
    parser = SafeArgumentParser(description="ERPClaw Inventory Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Item fields
    parser.add_argument("--item-id")
    parser.add_argument("--item-code")
    parser.add_argument("--item-name")
    parser.add_argument("--item-group")
    parser.add_argument("--item-type")
    parser.add_argument("--stock-uom")
    parser.add_argument("--valuation-method")
    parser.add_argument("--has-batch")
    parser.add_argument("--has-serial")
    parser.add_argument("--standard-rate")
    parser.add_argument("--reorder-level")
    parser.add_argument("--reorder-qty")
    parser.add_argument("--status", dest="item_status")

    # Item group
    parser.add_argument("--parent-id")
    parser.add_argument("--name")

    # Warehouse
    parser.add_argument("--warehouse-id")
    parser.add_argument("--warehouse-name", dest="name")  # alias for --name
    parser.add_argument("--warehouse-type")
    parser.add_argument("--account-id")
    parser.add_argument("--is-group")
    parser.add_argument("--company-id")
    parser.add_argument("--company", dest="company_name", default=None)  # NL: company by name
    parser.add_argument("--csv-path")

    # Stock entry
    parser.add_argument("--stock-entry-id")
    parser.add_argument("--entry-type")
    parser.add_argument("--posting-date")
    parser.add_argument("--items")  # JSON
    # S6 typed-dispatch parents
    parser.add_argument("--supplier-warehouse-id")  # send_to_subcontractor target
    parser.add_argument("--work-order-id")          # material_consumption parent
    # add-repack-stock-entry / add-material-consumption convenience-wrapper flags
    parser.add_argument("--warehouse")              # repack same-warehouse
    parser.add_argument("--from-item-id")
    parser.add_argument("--from-qty")
    parser.add_argument("--to-item-id")
    parser.add_argument("--to-qty")

    # Stock entry list filters
    parser.add_argument("--status-filter", dest="se_status")

    # Cross-skill SLE
    parser.add_argument("--voucher-type")
    parser.add_argument("--voucher-id")
    parser.add_argument("--entries")  # JSON

    # Batch
    parser.add_argument("--batch-name")
    parser.add_argument("--batch-id")
    parser.add_argument("--expiry-date")
    parser.add_argument("--manufacturing-date")

    # Serial number
    parser.add_argument("--serial-no")
    parser.add_argument("--serial-status", dest="sn_status")

    # Pricing
    parser.add_argument("--price-list-id")
    parser.add_argument("--rate")
    parser.add_argument("--min-qty")
    parser.add_argument("--max-qty")
    parser.add_argument("--valid-from")
    parser.add_argument("--valid-to")
    parser.add_argument("--qty", "--required-qty")  # --required-qty: S7 get-best-alternative
    parser.add_argument("--party-id")
    parser.add_argument("--currency")
    parser.add_argument("--is-buying")
    parser.add_argument("--is-selling")

    # Pricing rule
    parser.add_argument("--applies-to")
    parser.add_argument("--entity-id")
    parser.add_argument("--discount-percentage")
    parser.add_argument("--pricing-rule-rate", dest="pr_rate")
    parser.add_argument("--priority", type=int, default=None)

    # Stock reconciliation
    parser.add_argument("--stock-reconciliation-id")

    # Stock revaluation
    parser.add_argument("--revaluation-id")
    parser.add_argument("--new-rate")
    parser.add_argument("--reason")

    # Item variants
    parser.add_argument("--template-item-id")
    parser.add_argument("--attribute-name")
    parser.add_argument("--attribute-values")
    parser.add_argument("--attributes")  # JSON object for create-item-variant

    # Item supplier
    parser.add_argument("--supplier-id")
    parser.add_argument("--min-order-qty")
    parser.add_argument("--lead-time-days")

    # Wave 2 M5: putaway + pick list + reservation (ADR-0026)
    parser.add_argument("--id")  # putaway_rule / pick_list / reservation id
    parser.add_argument("--target-warehouse", dest="target_warehouse_id")
    parser.add_argument("--match-item", dest="match_item_id")
    parser.add_argument("--match-item-group", dest="match_item_group")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--stock-entry", dest="stock_entry_id")  # apply-putaway-on-receipt
    parser.add_argument("--from-sales-order", dest="sales_order_id")
    parser.add_argument("--pick-list", dest="pick_list_id")
    parser.add_argument("--source-bin")  # free-TEXT hint only (no bin schema in V1)
    parser.add_argument("--picked-qty")
    parser.add_argument("--reservation-status", dest="reservation_status")
    # Plan signatures use the short --item form (add-reservation,
    # add-pick-list-item). Register it explicitly so argparse resolves the exact
    # match instead of erroring on the --item-* abbrev. (--warehouse already
    # exists for repack with dest 'warehouse'; add-reservation reads warehouse
    # from --warehouse-id, falling back to --warehouse, in its handler.)
    parser.add_argument("--item", dest="item_id")
    # Wave 2 S7: item-global alternatives. --alternative is the substitute item;
    # --required-qty is registered as an alias on --qty (above).
    parser.add_argument("--alternative", dest="alternative_item_id")
    parser.add_argument("--conversion-factor")
    parser.add_argument("--notes")

    # Search / filters
    parser.add_argument("--search")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")
    parser.add_argument("--custom-fields", default=None,
                        help='User-defined fields as a JSON object, e.g. \'{"hs_code": "8471"}\'')

    args, unknown = parser.parse_known_args()
    check_unknown_args(parser, unknown)
    check_input_lengths(args)

    db_path = args.db_path or DEFAULT_DB_PATH
    ensure_db_exists(db_path)
    conn = get_connection(db_path)

    # Dependency check
    _dep = check_required_tables(conn, REQUIRED_TABLES)
    if _dep:
        _dep["suggestion"] = "clawhub install " + " ".join(_dep.get("missing_skills", []))
        print(json.dumps(_dep, indent=2))
        conn.close()
        sys.exit(1)

    try:
        ACTIONS[args.action](conn, args)
    except Exception as e:
        conn.rollback()
        sys.stderr.write(f"[erpclaw-inventory] {e}\n")
        err("An unexpected error occurred")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
