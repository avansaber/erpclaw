#!/usr/bin/env python3
"""ERPClaw Manufacturing Skill — db_query.py

BOMs, work orders, job cards, production planning (MRP), and subcontracting.
All 24 actions are routed through this single entry point.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# Add shared lib to path
try:
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
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
    )
    from erpclaw_lib.gl_posting import insert_gl_entries, reverse_gl_entries
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw-setup first: clawhub install erpclaw-setup", "suggestion": "clawhub install erpclaw-setup"}))
    sys.exit(1)

REQUIRED_TABLES = ["company", "item"]

VALID_WORKSTATION_STATUSES = ("active", "maintenance", "offline")
VALID_WO_STATUSES = (
    "draft", "not_started", "in_process", "completed", "stopped", "cancelled",
)
VALID_JOB_CARD_STATUSES = ("open", "in_process", "completed", "cancelled")
VALID_PP_STATUSES = ("draft", "submitted", "material_requested", "cancelled")
VALID_SCO_STATUSES = (
    "draft", "submitted", "partially_received", "completed", "cancelled",
)

# Maximum recursion depth for BOM explosion to prevent infinite loops
MAX_BOM_EXPLOSION_DEPTH = 10



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
    cc = conn.execute(
        "SELECT id FROM cost_center WHERE company_id = ? AND is_group = 0 LIMIT 1",
        (company_id,),
    ).fetchone()
    return cc["id"] if cc else None


def _validate_item_exists(conn, item_id: str, label: str = "Item"):
    """Validate that an item exists and return the row, or error."""
    item = conn.execute("SELECT * FROM item WHERE id = ?", (item_id,)).fetchone()
    if not item:
        err(f"{label} {item_id} not found")
    return item


def _validate_bom_exists(conn, bom_id: str):
    """Validate that a BOM exists and return the row, or error."""
    bom = conn.execute("SELECT * FROM bom WHERE id = ?", (bom_id,)).fetchone()
    if not bom:
        err(f"BOM {bom_id} not found",
             suggestion="Use 'list boms' to see available BOMs.")
    return bom


def _validate_company_exists(conn, company_id: str):
    """Validate that a company exists and return the row, or error."""
    company = conn.execute(
        "SELECT id FROM company WHERE id = ?", (company_id,),
    ).fetchone()
    if not company:
        err(f"Company {company_id} not found")
    return company


def _validate_operation_exists(conn, operation_id: str):
    """Validate that an operation exists and return the row, or error."""
    op = conn.execute(
        "SELECT * FROM operation WHERE id = ?", (operation_id,),
    ).fetchone()
    if not op:
        err(f"Operation {operation_id} not found")
    return op


def _validate_workstation_exists(conn, workstation_id: str):
    """Validate that a workstation exists and return the row, or error."""
    ws = conn.execute(
        "SELECT * FROM workstation WHERE id = ?", (workstation_id,),
    ).fetchone()
    if not ws:
        err(f"Workstation {workstation_id} not found")
    return ws


def _validate_warehouse_exists(conn, warehouse_id: str, label: str = "Warehouse"):
    """Validate that a warehouse exists and return the row, or error."""
    wh = conn.execute(
        "SELECT * FROM warehouse WHERE id = ?", (warehouse_id,),
    ).fetchone()
    if not wh:
        err(f"{label} {warehouse_id} not found")
    return wh


def _calculate_bom_item_costs(items_data: list) -> Decimal:
    """Calculate total raw material cost from a list of BOM item dicts.

    Each item dict must have "quantity" and "rate" keys (string Decimal values).
    Returns the sum of (quantity * rate) for all items, rounded.
    """
    total = Decimal("0")
    for item in items_data:
        qty = to_decimal(item.get("quantity", "0"))
        rate = to_decimal(item.get("rate", "0"))
        total += qty * rate
    return round_currency(total)


def _calculate_operation_costs(conn, operations_data: list) -> Decimal:
    """Calculate total operating cost from a list of operation dicts.

    Each operation dict should have time_in_minutes and optionally
    operating_cost (overrides workstation hourly rate lookup).
    Returns the sum of (time_in_minutes / 60 * cost_per_hour), rounded.
    """
    total = Decimal("0")
    for op in operations_data:
        explicit_cost = op.get("operating_cost")
        if explicit_cost and to_decimal(explicit_cost) > 0:
            total += to_decimal(explicit_cost)
        else:
            # Calculate from workstation hourly rate
            time_mins = to_decimal(op.get("time_in_minutes", "0"))
            ws_id = op.get("workstation_id")
            if ws_id and time_mins > 0:
                ws = conn.execute(
                    "SELECT operating_cost_per_hour FROM workstation WHERE id = ?",
                    (ws_id,),
                ).fetchone()
                if ws:
                    hour_rate = to_decimal(ws["operating_cost_per_hour"])
                    cost = round_currency((time_mins / Decimal("60")) * hour_rate)
                    total += cost
    return round_currency(total)


# ---------------------------------------------------------------------------
# 1. add-bom
# ---------------------------------------------------------------------------

def add_bom(conn, args):
    """Create a Bill of Materials.

    Required: --item-id (finished goods), --items (JSON), --company-id
    Optional: --quantity (default "1"), --operations (JSON), --routing-id,
              --is-default, --uom
    """
    if not args.item_id:
        err("--item-id is required (finished goods item)")
    if not args.items:
        err("--items is required (JSON array of raw materials)")
    if not args.company_id:
        err("--company-id is required")

    # Validate finished goods item
    fg_item = _validate_item_exists(conn, args.item_id, "Finished goods item")

    # Validate company
    _validate_company_exists(conn, args.company_id)

    # Parse and validate items JSON
    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    bom_qty = to_decimal(args.quantity or "1")
    if bom_qty <= 0:
        err("--quantity must be greater than 0")

    # Generate naming series
    naming = get_next_name(conn, "bom", company_id=args.company_id)

    # Validate and prepare BOM item rows
    bom_item_rows = []
    for i, item in enumerate(items):
        rm_item_id = item.get("item_id")
        if not rm_item_id:
            err(f"Item {i}: item_id is required")

        # Validate raw material item exists
        _validate_item_exists(conn, rm_item_id, f"Item {i}: raw material item")

        rm_qty = to_decimal(item.get("quantity", "0"))
        if rm_qty <= 0:
            err(f"Item {i}: quantity must be > 0")

        rm_rate = to_decimal(item.get("rate", "0"))
        if rm_rate < 0:
            err(f"Item {i}: rate must be >= 0")

        rm_amount = round_currency(rm_qty * rm_rate)
        rm_uom = item.get("uom")
        is_sub_assembly = int(item.get("is_sub_assembly", 0))
        sub_bom_id = item.get("sub_bom_id")
        source_warehouse_id = item.get("source_warehouse_id")
        scrap_percentage = item.get("scrap_percentage", "0")

        # Validate sub-BOM reference if this is a sub-assembly
        if is_sub_assembly and sub_bom_id:
            sub_bom = conn.execute(
                "SELECT id, item_id FROM bom WHERE id = ?", (sub_bom_id,),
            ).fetchone()
            if not sub_bom:
                err(f"Item {i}: sub_bom_id {sub_bom_id} not found")
            if sub_bom["item_id"] != rm_item_id:
                err(
                    f"Item {i}: sub_bom_id {sub_bom_id} produces item "
                    f"{sub_bom['item_id']}, but item_id is {rm_item_id}"
                )

        # Validate source warehouse if provided
        if source_warehouse_id:
            _validate_warehouse_exists(
                conn, source_warehouse_id,
                f"Item {i}: source warehouse",
            )

        bom_item_rows.append({
            "id": str(uuid.uuid4()),
            "item_id": rm_item_id,
            "quantity": str(round_currency(rm_qty)),
            "uom": rm_uom,
            "rate": str(round_currency(rm_rate)),
            "amount": str(rm_amount),
            "source_warehouse_id": source_warehouse_id,
            "is_sub_assembly": is_sub_assembly,
            "sub_bom_id": sub_bom_id,
            "scrap_percentage": str(to_decimal(scrap_percentage)),
        })

    # Calculate raw material cost
    raw_material_cost = Decimal("0")
    for row in bom_item_rows:
        raw_material_cost += to_decimal(row["amount"])
    raw_material_cost = round_currency(raw_material_cost)

    # Handle operations: from --routing-id or --operations JSON
    operations = _parse_json_arg(args.operations, "operations") if args.operations else None
    routing_id = args.routing_id

    bom_operation_rows = []
    with_operations = 0

    if routing_id:
        # Copy operations from routing
        routing = conn.execute(
            "SELECT id FROM routing WHERE id = ?", (routing_id,),
        ).fetchone()
        if not routing:
            err(f"Routing {routing_id} not found")

        routing_ops = conn.execute(
            """SELECT operation_id, workstation_id, sequence,
                      time_in_minutes, operating_cost
               FROM routing_operation WHERE routing_id = ?
               ORDER BY sequence""",
            (routing_id,),
        ).fetchall()
        if not routing_ops:
            err(f"Routing {routing_id} has no operations")

        for ro in routing_ops:
            ro_dict = row_to_dict(ro)
            # If no explicit operating_cost, calculate from workstation
            op_cost = to_decimal(ro_dict.get("operating_cost", "0"))
            if op_cost <= 0 and ro_dict.get("workstation_id"):
                ws = conn.execute(
                    "SELECT operating_cost_per_hour FROM workstation WHERE id = ?",
                    (ro_dict["workstation_id"],),
                ).fetchone()
                if ws:
                    time_mins = to_decimal(ro_dict.get("time_in_minutes", "0"))
                    op_cost = round_currency(
                        (time_mins / Decimal("60"))
                        * to_decimal(ws["operating_cost_per_hour"])
                    )

            bom_operation_rows.append({
                "id": str(uuid.uuid4()),
                "operation_id": ro_dict["operation_id"],
                "workstation_id": ro_dict.get("workstation_id"),
                "sequence": ro_dict.get("sequence", 0),
                "time_in_minutes": ro_dict.get("time_in_minutes", "0"),
                "operating_cost": str(round_currency(op_cost)),
                "description": None,
            })
        with_operations = 1

    elif operations:
        # Inline operations from JSON
        if not isinstance(operations, list):
            err("--operations must be a JSON array")

        for i, op in enumerate(operations):
            op_id_ref = op.get("operation_id")
            if not op_id_ref:
                err(f"Operation {i}: operation_id is required")

            _validate_operation_exists(conn, op_id_ref)

            ws_id = op.get("workstation_id")
            if ws_id:
                _validate_workstation_exists(conn, ws_id)

            # Calculate operating cost if not provided
            op_cost = to_decimal(op.get("operating_cost", "0"))
            if op_cost <= 0 and ws_id:
                ws = conn.execute(
                    "SELECT operating_cost_per_hour FROM workstation WHERE id = ?",
                    (ws_id,),
                ).fetchone()
                if ws:
                    time_mins = to_decimal(op.get("time_in_minutes", "0"))
                    op_cost = round_currency(
                        (time_mins / Decimal("60"))
                        * to_decimal(ws["operating_cost_per_hour"])
                    )

            bom_operation_rows.append({
                "id": str(uuid.uuid4()),
                "operation_id": op_id_ref,
                "workstation_id": ws_id,
                "sequence": op.get("sequence", i + 1),
                "time_in_minutes": op.get("time_in_minutes", "0"),
                "operating_cost": str(round_currency(op_cost)),
                "description": op.get("description"),
            })
        with_operations = 1

    # Calculate operating cost
    operating_cost = Decimal("0")
    for op_row in bom_operation_rows:
        operating_cost += to_decimal(op_row["operating_cost"])
    operating_cost = round_currency(operating_cost)

    # Total cost = raw material + operating
    total_cost = round_currency(raw_material_cost + operating_cost)

    # Determine is_default
    is_default = int(args.is_default) if args.is_default else 0

    # If this is the first BOM for this item in this company, make it default
    if not is_default:
        existing_bom = conn.execute(
            "SELECT id FROM bom WHERE item_id = ? AND company_id = ? AND is_active = 1 LIMIT 1",
            (args.item_id, args.company_id),
        ).fetchone()
        if not existing_bom:
            is_default = 1

    # If setting as default, unset previous defaults for this item+company
    if is_default:
        conn.execute(
            """UPDATE bom SET is_default = 0, updated_at = datetime('now')
               WHERE item_id = ? AND company_id = ? AND is_default = 1""",
            (args.item_id, args.company_id),
        )

    bom_id = str(uuid.uuid4())
    fg_dict = row_to_dict(fg_item)
    uom = args.uom or fg_dict.get("stock_uom")

    # Insert BOM parent
    try:
        conn.execute(
            """INSERT INTO bom
               (id, naming_series, item_id, quantity, uom, is_active, is_default,
                operating_cost, raw_material_cost, total_cost, with_operations,
                routing_id, company_id)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (bom_id, naming, args.item_id, str(round_currency(bom_qty)),
             uom, is_default,
             str(operating_cost), str(raw_material_cost), str(total_cost),
             with_operations, routing_id, args.company_id),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("BOM creation failed — check for duplicates or invalid data")

    # Insert BOM item rows
    for row in bom_item_rows:
        conn.execute(
            """INSERT INTO bom_item
               (id, bom_id, item_id, quantity, uom, rate, amount,
                source_warehouse_id, is_sub_assembly, sub_bom_id, scrap_percentage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], bom_id, row["item_id"], row["quantity"],
             row["uom"], row["rate"], row["amount"],
             row["source_warehouse_id"], row["is_sub_assembly"],
             row["sub_bom_id"], row["scrap_percentage"]),
        )

    # Insert BOM operation rows
    for op_row in bom_operation_rows:
        conn.execute(
            """INSERT INTO bom_operation
               (id, bom_id, operation_id, workstation_id,
                time_in_minutes, operating_cost, sequence, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (op_row["id"], bom_id, op_row["operation_id"],
             op_row["workstation_id"], op_row["time_in_minutes"],
             op_row["operating_cost"], op_row["sequence"],
             op_row["description"]),
        )

    audit(conn, "erpclaw-manufacturing", "add-bom", "bom", bom_id,
           new_values={
               "naming_series": naming, "item_id": args.item_id,
               "quantity": str(round_currency(bom_qty)),
               "item_count": len(bom_item_rows),
               "operation_count": len(bom_operation_rows),
               "total_cost": str(total_cost),
           })
    conn.commit()

    ok({
        "bom_id": bom_id,
        "naming_series": naming,
        "item_id": args.item_id,
        "quantity": str(round_currency(bom_qty)),
        "raw_material_cost": str(raw_material_cost),
        "operating_cost": str(operating_cost),
        "total_cost": str(total_cost),
        "item_count": len(bom_item_rows),
        "operation_count": len(bom_operation_rows),
        "is_default": is_default,
    })


# ---------------------------------------------------------------------------
# 2. update-bom
# ---------------------------------------------------------------------------

def update_bom(conn, args):
    """Update an existing Bill of Materials.

    Required: --bom-id
    Optional: --items (replace all), --operations (replace all),
              --quantity, --is-active, --is-default, --routing-id
    """
    if not args.bom_id:
        err("--bom-id is required")

    bom = _validate_bom_exists(conn, args.bom_id)
    bom_dict = row_to_dict(bom)
    company_id = bom_dict["company_id"]
    item_id = bom_dict["item_id"]

    updates = []
    params = []
    updated_fields = []
    recalculate_costs = False

    # Update quantity
    if args.quantity is not None:
        new_qty = to_decimal(args.quantity)
        if new_qty <= 0:
            err("--quantity must be greater than 0")
        updates.append("quantity = ?")
        params.append(str(round_currency(new_qty)))
        updated_fields.append("quantity")
        recalculate_costs = True

    # Update is_active
    if args.is_active is not None:
        is_active = int(args.is_active)
        if is_active not in (0, 1):
            err("--is-active must be 0 or 1")
        updates.append("is_active = ?")
        params.append(is_active)
        updated_fields.append("is_active")

    # Update is_default
    if args.is_default is not None:
        is_default = int(args.is_default)
        if is_default not in (0, 1):
            err("--is-default must be 0 or 1")
        if is_default:
            # Unset previous defaults for this item+company
            conn.execute(
                """UPDATE bom SET is_default = 0, updated_at = datetime('now')
                   WHERE item_id = ? AND company_id = ? AND is_default = 1 AND id != ?""",
                (item_id, company_id, args.bom_id),
            )
        updates.append("is_default = ?")
        params.append(is_default)
        updated_fields.append("is_default")

    # Update routing
    if args.routing_id is not None:
        if args.routing_id == "":
            # Clear routing
            updates.append("routing_id = NULL")
            updated_fields.append("routing_id")
        else:
            routing = conn.execute(
                "SELECT id FROM routing WHERE id = ?", (args.routing_id,),
            ).fetchone()
            if not routing:
                err(f"Routing {args.routing_id} not found")
            updates.append("routing_id = ?")
            params.append(args.routing_id)
            updated_fields.append("routing_id")

    # Replace items if provided
    new_raw_material_cost = None
    if args.items is not None:
        items = _parse_json_arg(args.items, "items")
        if not items or not isinstance(items, list):
            err("--items must be a non-empty JSON array")

        # Validate all new items before deleting old ones
        new_bom_items = []
        for i, item in enumerate(items):
            rm_item_id = item.get("item_id")
            if not rm_item_id:
                err(f"Item {i}: item_id is required")
            _validate_item_exists(conn, rm_item_id, f"Item {i}: raw material item")

            rm_qty = to_decimal(item.get("quantity", "0"))
            if rm_qty <= 0:
                err(f"Item {i}: quantity must be > 0")

            rm_rate = to_decimal(item.get("rate", "0"))
            if rm_rate < 0:
                err(f"Item {i}: rate must be >= 0")

            rm_amount = round_currency(rm_qty * rm_rate)
            is_sub_assembly = int(item.get("is_sub_assembly", 0))
            sub_bom_id = item.get("sub_bom_id")
            source_warehouse_id = item.get("source_warehouse_id")

            if is_sub_assembly and sub_bom_id:
                sub_bom = conn.execute(
                    "SELECT id, item_id FROM bom WHERE id = ?", (sub_bom_id,),
                ).fetchone()
                if not sub_bom:
                    err(f"Item {i}: sub_bom_id {sub_bom_id} not found")
                if sub_bom["item_id"] != rm_item_id:
                    err(
                        f"Item {i}: sub_bom_id {sub_bom_id} produces item "
                        f"{sub_bom['item_id']}, but item_id is {rm_item_id}"
                    )

            if source_warehouse_id:
                _validate_warehouse_exists(
                    conn, source_warehouse_id, f"Item {i}: source warehouse",
                )

            new_bom_items.append({
                "id": str(uuid.uuid4()),
                "item_id": rm_item_id,
                "quantity": str(round_currency(rm_qty)),
                "uom": item.get("uom"),
                "rate": str(round_currency(rm_rate)),
                "amount": str(rm_amount),
                "source_warehouse_id": source_warehouse_id,
                "is_sub_assembly": is_sub_assembly,
                "sub_bom_id": sub_bom_id,
                "scrap_percentage": str(to_decimal(item.get("scrap_percentage", "0"))),
            })

        # Delete old items and insert new ones
        conn.execute("DELETE FROM bom_item WHERE bom_id = ?", (args.bom_id,))
        for row in new_bom_items:
            conn.execute(
                """INSERT INTO bom_item
                   (id, bom_id, item_id, quantity, uom, rate, amount,
                    source_warehouse_id, is_sub_assembly, sub_bom_id, scrap_percentage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["id"], args.bom_id, row["item_id"], row["quantity"],
                 row["uom"], row["rate"], row["amount"],
                 row["source_warehouse_id"], row["is_sub_assembly"],
                 row["sub_bom_id"], row["scrap_percentage"]),
            )

        # Recalculate raw material cost
        new_raw_material_cost = Decimal("0")
        for row in new_bom_items:
            new_raw_material_cost += to_decimal(row["amount"])
        new_raw_material_cost = round_currency(new_raw_material_cost)
        updated_fields.append("items")
        recalculate_costs = True

    # Replace operations if provided
    new_operating_cost = None
    if args.operations is not None:
        operations = _parse_json_arg(args.operations, "operations")

        # Delete old operations
        conn.execute("DELETE FROM bom_operation WHERE bom_id = ?", (args.bom_id,))

        if operations and isinstance(operations, list):
            for i, op in enumerate(operations):
                op_id_ref = op.get("operation_id")
                if not op_id_ref:
                    err(f"Operation {i}: operation_id is required")
                _validate_operation_exists(conn, op_id_ref)

                ws_id = op.get("workstation_id")
                if ws_id:
                    _validate_workstation_exists(conn, ws_id)

                # Calculate operating cost if not provided
                op_cost = to_decimal(op.get("operating_cost", "0"))
                if op_cost <= 0 and ws_id:
                    ws = conn.execute(
                        "SELECT operating_cost_per_hour FROM workstation WHERE id = ?",
                        (ws_id,),
                    ).fetchone()
                    if ws:
                        time_mins = to_decimal(op.get("time_in_minutes", "0"))
                        op_cost = round_currency(
                            (time_mins / Decimal("60"))
                            * to_decimal(ws["operating_cost_per_hour"])
                        )

                conn.execute(
                    """INSERT INTO bom_operation
                       (id, bom_id, operation_id, workstation_id,
                        time_in_minutes, operating_cost, sequence, description)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), args.bom_id, op_id_ref, ws_id,
                     op.get("time_in_minutes", "0"),
                     str(round_currency(op_cost)),
                     op.get("sequence", i + 1),
                     op.get("description")),
                )

            updates.append("with_operations = 1")
            # Calculate new operating cost
            new_operating_cost = Decimal("0")
            op_rows = conn.execute(
                "SELECT operating_cost FROM bom_operation WHERE bom_id = ?",
                (args.bom_id,),
            ).fetchall()
            for op_row in op_rows:
                new_operating_cost += to_decimal(op_row["operating_cost"])
            new_operating_cost = round_currency(new_operating_cost)
        else:
            # Empty operations list: clear operations
            updates.append("with_operations = 0")
            new_operating_cost = Decimal("0")

        updated_fields.append("operations")
        recalculate_costs = True

    # Recalculate costs if needed
    if recalculate_costs:
        # Get current costs if not being replaced
        if new_raw_material_cost is None:
            new_raw_material_cost = to_decimal(bom_dict["raw_material_cost"])
        if new_operating_cost is None:
            new_operating_cost = to_decimal(bom_dict["operating_cost"])

        total_cost = round_currency(new_raw_material_cost + new_operating_cost)

        updates.append("raw_material_cost = ?")
        params.append(str(new_raw_material_cost))
        updates.append("operating_cost = ?")
        params.append(str(new_operating_cost))
        updates.append("total_cost = ?")
        params.append(str(total_cost))

    if not updated_fields:
        err("No fields to update")

    updates.append("updated_at = datetime('now')")
    params.append(args.bom_id)
    conn.execute(
        f"UPDATE bom SET {', '.join(updates)} WHERE id = ?", params,
    )

    audit(conn, "erpclaw-manufacturing", "update-bom", "bom", args.bom_id,
           new_values={"updated_fields": updated_fields})
    conn.commit()

    # Fetch updated BOM for response
    updated_bom = conn.execute(
        "SELECT * FROM bom WHERE id = ?", (args.bom_id,),
    ).fetchone()
    updated_dict = row_to_dict(updated_bom)

    ok({
        "bom_id": args.bom_id,
        "updated_fields": updated_fields,
        "raw_material_cost": updated_dict["raw_material_cost"],
        "operating_cost": updated_dict["operating_cost"],
        "total_cost": updated_dict["total_cost"],
    })


# ---------------------------------------------------------------------------
# 3. get-bom
# ---------------------------------------------------------------------------

def get_bom(conn, args):
    """Get BOM with items and operations.

    Required: --bom-id
    Returns: BOM header + items list + operations list
    """
    if not args.bom_id:
        err("--bom-id is required")

    bom = conn.execute("SELECT * FROM bom WHERE id = ?", (args.bom_id,)).fetchone()
    if not bom:
        err(f"BOM {args.bom_id} not found")

    data = row_to_dict(bom)

    # Fetch finished goods item details
    fg_item = conn.execute(
        "SELECT item_code, item_name, stock_uom FROM item WHERE id = ?",
        (data["item_id"],),
    ).fetchone()
    if fg_item:
        fg_dict = row_to_dict(fg_item)
        data["item_code"] = fg_dict.get("item_code")
        data["item_name"] = fg_dict.get("item_name")
        data["stock_uom"] = fg_dict.get("stock_uom")

    # Fetch BOM items with item details
    items = conn.execute(
        """SELECT bi.*, i.item_code, i.item_name, i.stock_uom AS item_uom
           FROM bom_item bi
           LEFT JOIN item i ON i.id = bi.item_id
           WHERE bi.bom_id = ?
           ORDER BY bi.rowid""",
        (args.bom_id,),
    ).fetchall()
    data["items"] = [row_to_dict(r) for r in items]

    # Fetch BOM operations with operation and workstation details
    operations = conn.execute(
        """SELECT bo.*, o.name AS operation_name,
                  w.name AS workstation_name,
                  w.operating_cost_per_hour AS ws_hour_rate
           FROM bom_operation bo
           LEFT JOIN operation o ON o.id = bo.operation_id
           LEFT JOIN workstation w ON w.id = bo.workstation_id
           WHERE bo.bom_id = ?
           ORDER BY bo.sequence, bo.rowid""",
        (args.bom_id,),
    ).fetchall()
    data["operations"] = [row_to_dict(r) for r in operations]

    ok(data)


# ---------------------------------------------------------------------------
# 4. list-boms
# ---------------------------------------------------------------------------

def list_boms(conn, args):
    """Query BOMs with filtering.

    Optional: --item-id, --is-active, --company-id, --is-default,
              --search, --limit (20), --offset (0)
    """
    conditions = ["1=1"]
    params = []

    if args.item_id:
        conditions.append("b.item_id = ?")
        params.append(args.item_id)
    if args.is_active is not None:
        conditions.append("b.is_active = ?")
        params.append(int(args.is_active))
    if args.company_id:
        conditions.append("b.company_id = ?")
        params.append(args.company_id)
    if args.is_default is not None:
        conditions.append("b.is_default = ?")
        params.append(int(args.is_default))
    if args.search:
        conditions.append(
            "(i.item_name LIKE ? OR i.item_code LIKE ? OR b.naming_series LIKE ?)"
        )
        params.extend([f"%{args.search}%", f"%{args.search}%", f"%{args.search}%"])

    where = " AND ".join(conditions)

    count_row = conn.execute(
        f"""SELECT COUNT(*) FROM bom b
            LEFT JOIN item i ON i.id = b.item_id
            WHERE {where}""",
        params,
    ).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    params.extend([limit, offset])

    rows = conn.execute(
        f"""SELECT b.id, b.naming_series, b.item_id, b.quantity, b.is_active,
               b.is_default, b.raw_material_cost, b.operating_cost,
               b.total_cost, b.with_operations, b.company_id,
               i.item_code, i.item_name
           FROM bom b
           LEFT JOIN item i ON i.id = b.item_id
           WHERE {where}
           ORDER BY b.created_at DESC
           LIMIT ? OFFSET ?""",
        params,
    ).fetchall()

    ok({"boms": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 5. explode-bom
# ---------------------------------------------------------------------------

def explode_bom(conn, args):
    """Recursive multi-level BOM explosion.

    Required: --bom-id, --quantity (how many FG units to produce)
    Returns: flat list of leaf raw materials with total required quantities

    Logic:
    1. Fetch BOM + items
    2. For each item: if is_sub_assembly and sub_bom_id -> recurse into sub-BOM
    3. Otherwise add to flat list with scaled quantity
    4. Limit recursion depth to 10; detect circular references
    5. Return flat list of leaf raw materials with total required quantities
    """
    if not args.bom_id:
        err("--bom-id is required")

    requested_qty = to_decimal(args.quantity or "1")
    if requested_qty <= 0:
        err("--quantity must be greater than 0")

    bom = _validate_bom_exists(conn, args.bom_id)
    bom_dict = row_to_dict(bom)

    # Track visited BOMs for circular reference detection
    visited = set()

    # Accumulate leaf materials: {item_id: {"item_id", "item_code", "item_name", "uom", "total_qty"}}
    leaf_materials = {}

    def _explode_recursive(bom_id: str, parent_qty: Decimal, depth: int):
        """Recursively explode a BOM, accumulating leaf materials."""
        if depth > MAX_BOM_EXPLOSION_DEPTH:
            err(
                f"BOM explosion exceeded maximum depth of {MAX_BOM_EXPLOSION_DEPTH}. "
                f"Check for overly nested sub-assemblies."
            )

        if bom_id in visited:
            err(
                f"Circular reference detected: BOM {bom_id} appears in its own "
                f"sub-assembly chain. Fix the BOM structure to remove the cycle."
            )

        visited.add(bom_id)

        # Fetch the BOM header for its quantity (yield per BOM)
        current_bom = conn.execute(
            "SELECT quantity FROM bom WHERE id = ?", (bom_id,),
        ).fetchone()
        if not current_bom:
            err(f"BOM {bom_id} not found during explosion")

        bom_base_qty = to_decimal(current_bom["quantity"])
        if bom_base_qty <= 0:
            err(f"BOM {bom_id} has invalid quantity: {bom_base_qty}")

        # Fetch BOM items
        bom_items = conn.execute(
            """SELECT bi.*, i.item_code, i.item_name, i.stock_uom
               FROM bom_item bi
               LEFT JOIN item i ON i.id = bi.item_id
               WHERE bi.bom_id = ?""",
            (bom_id,),
        ).fetchall()

        for bi_row in bom_items:
            bi = row_to_dict(bi_row)
            item_qty = to_decimal(bi["quantity"])

            # Scale quantity: (item.qty / bom.qty) * parent_qty
            scaled_qty = round_currency((item_qty / bom_base_qty) * parent_qty)

            # Apply scrap percentage: effective_qty = scaled_qty * (1 + scrap% / 100)
            scrap_pct = to_decimal(bi.get("scrap_percentage", "0"))
            if scrap_pct > 0:
                scaled_qty = round_currency(
                    scaled_qty * (Decimal("1") + scrap_pct / Decimal("100"))
                )

            if bi.get("is_sub_assembly") and bi.get("sub_bom_id"):
                # Recurse into sub-assembly BOM
                _explode_recursive(bi["sub_bom_id"], scaled_qty, depth + 1)
            else:
                # Leaf material: accumulate
                rm_item_id = bi["item_id"]
                if rm_item_id in leaf_materials:
                    leaf_materials[rm_item_id]["total_qty"] = round_currency(
                        to_decimal(leaf_materials[rm_item_id]["total_qty"]) + scaled_qty
                    )
                else:
                    leaf_materials[rm_item_id] = {
                        "item_id": rm_item_id,
                        "item_code": bi.get("item_code", ""),
                        "item_name": bi.get("item_name", ""),
                        "uom": bi.get("stock_uom") or bi.get("uom", ""),
                        "total_qty": round_currency(scaled_qty),
                    }

        # Remove from visited after processing to allow same BOM in
        # different branches (sibling sub-assemblies using the same BOM)
        visited.discard(bom_id)

    # Start explosion from the root BOM
    _explode_recursive(args.bom_id, requested_qty, depth=0)

    # Convert to list, sorted by item_code for consistent output
    materials_list = sorted(
        [
            {
                "item_id": mat["item_id"],
                "item_code": mat["item_code"],
                "item_name": mat["item_name"],
                "uom": mat["uom"],
                "total_qty": str(mat["total_qty"]),
            }
            for mat in leaf_materials.values()
        ],
        key=lambda x: x.get("item_code", ""),
    )

    ok({
        "bom_id": args.bom_id,
        "fg_item_id": bom_dict["item_id"],
        "requested_qty": str(round_currency(requested_qty)),
        "materials": materials_list,
        "material_count": len(materials_list),
    })


# ---------------------------------------------------------------------------
# 6. add-operation
# ---------------------------------------------------------------------------

def add_operation(conn, args):
    """Create a manufacturing operation.

    Required: --name
    Optional: --workstation-id, --description
    """
    if not args.name:
        err("--name is required")

    # Check uniqueness
    existing = conn.execute(
        "SELECT id FROM operation WHERE name = ?", (args.name,),
    ).fetchone()
    if existing:
        err(f"Operation '{args.name}' already exists")

    # Validate workstation if provided
    ws_id = args.workstation_id
    if ws_id:
        _validate_workstation_exists(conn, ws_id)

    op_id = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO operation
               (id, name, description, default_workstation_id, is_active)
               VALUES (?, ?, ?, ?, 1)""",
            (op_id, args.name, args.description, ws_id),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("Operation creation failed — check for duplicates or invalid data")

    audit(conn, "erpclaw-manufacturing", "add-operation", "operation", op_id,
           new_values={"name": args.name, "workstation_id": ws_id})
    conn.commit()
    ok({"operation_id": op_id, "name": args.name})


# ---------------------------------------------------------------------------
# 7. add-workstation
# ---------------------------------------------------------------------------

def add_workstation(conn, args):
    """Create a workstation.

    Required: --name
    Optional: --workstation-type, --hour-rate (operating_cost_per_hour),
              --working-hours-per-day, --production-capacity,
              --holiday-list-id, --description
    """
    if not args.name:
        err("--name is required")

    existing = conn.execute(
        "SELECT id FROM workstation WHERE name = ?", (args.name,),
    ).fetchone()
    if existing:
        err(f"Workstation '{args.name}' already exists")

    hour_rate = str(round_currency(to_decimal(args.hour_rate or "0")))
    working_hours = args.working_hours_per_day

    # Validate holiday list if provided
    if args.holiday_list_id:
        hl = conn.execute(
            "SELECT id FROM holiday_list WHERE id = ?",
            (args.holiday_list_id,),
        ).fetchone()
        if not hl:
            err(f"Holiday list {args.holiday_list_id} not found")

    ws_id = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO workstation
               (id, name, workstation_type, production_capacity,
                operating_cost_per_hour, working_hours_per_day,
                holiday_list_id, status, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (ws_id, args.name, args.workstation_type,
             args.production_capacity, hour_rate,
             working_hours, args.holiday_list_id,
             args.description),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("Workstation creation failed — check for duplicates or invalid data")

    audit(conn, "erpclaw-manufacturing", "add-workstation", "workstation", ws_id,
           new_values={"name": args.name, "hour_rate": hour_rate})
    conn.commit()
    ok({"workstation_id": ws_id, "name": args.name,
         "operating_cost_per_hour": hour_rate})


# ---------------------------------------------------------------------------
# 8. add-routing
# ---------------------------------------------------------------------------

def add_routing(conn, args):
    """Create a routing with operations.

    Required: --name, --operations (JSON array)
    Optional: --description

    Operations JSON format:
    [
        {
            "operation_id": "...",
            "workstation_id": "..." (optional),
            "sequence": 1 (optional, defaults to position),
            "time_in_minutes": "30",
            "operating_cost": "0" (optional, calculated from workstation if omitted)
        }
    ]
    """
    if not args.name:
        err("--name is required")
    if not args.operations:
        err("--operations (JSON) is required")

    existing = conn.execute(
        "SELECT id FROM routing WHERE name = ?", (args.name,),
    ).fetchone()
    if existing:
        err(f"Routing '{args.name}' already exists")

    operations = _parse_json_arg(args.operations, "operations")
    if not operations or not isinstance(operations, list):
        err("--operations must be a non-empty JSON array")

    routing_id = str(uuid.uuid4())

    try:
        conn.execute(
            "INSERT INTO routing (id, name, description) VALUES (?, ?, ?)",
            (routing_id, args.name, args.description),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("Routing creation failed — check for duplicates or invalid data")

    for i, op in enumerate(operations):
        op_id_ref = op.get("operation_id")
        if not op_id_ref:
            err(f"Operation {i}: operation_id is required")

        _validate_operation_exists(conn, op_id_ref)

        ws_id = op.get("workstation_id")
        if ws_id:
            _validate_workstation_exists(conn, ws_id)

        # Calculate operating cost if not explicitly provided
        op_cost = to_decimal(op.get("operating_cost", "0"))
        if op_cost <= 0 and ws_id:
            ws = conn.execute(
                "SELECT operating_cost_per_hour FROM workstation WHERE id = ?",
                (ws_id,),
            ).fetchone()
            if ws:
                time_mins = to_decimal(op.get("time_in_minutes", "0"))
                op_cost = round_currency(
                    (time_mins / Decimal("60"))
                    * to_decimal(ws["operating_cost_per_hour"])
                )

        ro_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO routing_operation
               (id, routing_id, operation_id, workstation_id,
                sequence, time_in_minutes, operating_cost)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ro_id, routing_id, op_id_ref, ws_id,
             op.get("sequence", i + 1),
             op.get("time_in_minutes", "0"),
             str(round_currency(op_cost))),
        )

    audit(conn, "erpclaw-manufacturing", "add-routing", "routing", routing_id,
           new_values={"name": args.name, "operations_count": len(operations)})
    conn.commit()
    ok({
        "routing_id": routing_id,
        "name": args.name,
        "operations_count": len(operations),
    })


# ---------------------------------------------------------------------------
# 9. add-work-order
# ---------------------------------------------------------------------------

def add_work_order(conn, args):
    """Create a Work Order from a BOM.

    Required: --bom-id, --quantity, --company-id
    Optional: --planned-start-date, --planned-end-date,
              --source-warehouse-id, --target-warehouse-id, --wip-warehouse-id,
              --sales-order-id
    """
    if not args.bom_id:
        err("--bom-id is required")
    if not args.quantity:
        err("--quantity is required")
    if not args.company_id:
        err("--company-id is required")

    qty = to_decimal(args.quantity)
    if qty <= 0:
        err("--quantity must be greater than 0")

    # Validate BOM exists and is active
    bom = _validate_bom_exists(conn, args.bom_id)
    bom_dict = row_to_dict(bom)
    if not bom_dict.get("is_active"):
        err(f"BOM {args.bom_id} is not active")

    # The FG item comes from the BOM
    fg_item_id = bom_dict["item_id"]

    # Validate company
    _validate_company_exists(conn, args.company_id)

    # Validate warehouses if provided
    source_wh = args.source_warehouse_id
    target_wh = args.target_warehouse_id
    wip_wh = args.wip_warehouse_id
    if source_wh:
        _validate_warehouse_exists(conn, source_wh, "Source warehouse")
    if target_wh:
        _validate_warehouse_exists(conn, target_wh, "Target warehouse")
    if wip_wh:
        _validate_warehouse_exists(conn, wip_wh, "WIP warehouse")

    # Generate naming series
    naming = get_next_name(conn, "work_order", company_id=args.company_id)

    wo_id = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO work_order
               (id, naming_series, item_id, bom_id, qty, produced_qty,
                planned_start_date, planned_end_date,
                source_warehouse_id, target_warehouse_id, wip_warehouse_id,
                sales_order_id, status, material_transferred_for_manufacturing,
                company_id)
               VALUES (?, ?, ?, ?, ?, '0', ?, ?, ?, ?, ?, ?, 'draft', '0', ?)""",
            (wo_id, naming, fg_item_id, args.bom_id, str(round_currency(qty)),
             args.planned_start_date, args.planned_end_date,
             source_wh, target_wh, wip_wh,
             args.sales_order_id, args.company_id),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("Work order creation failed — check for duplicates or invalid data")

    # Copy BOM items to work_order_item with scaled quantities
    bom_qty = to_decimal(bom_dict["quantity"])
    if bom_qty <= 0:
        bom_qty = Decimal("1")

    bom_items = conn.execute(
        """SELECT bi.item_id, bi.quantity, bi.source_warehouse_id
           FROM bom_item bi WHERE bi.bom_id = ?""",
        (args.bom_id,),
    ).fetchall()

    for bi_row in bom_items:
        bi = row_to_dict(bi_row)
        bi_qty = to_decimal(bi["quantity"])
        required_qty = round_currency((bi_qty / bom_qty) * qty)
        woi_source_wh = bi.get("source_warehouse_id") or source_wh

        conn.execute(
            """INSERT INTO work_order_item
               (id, work_order_id, item_id, required_qty, transferred_qty,
                consumed_qty, source_warehouse_id)
               VALUES (?, ?, ?, ?, '0', '0', ?)""",
            (str(uuid.uuid4()), wo_id, bi["item_id"],
             str(required_qty), woi_source_wh),
        )

    audit(conn, "erpclaw-manufacturing", "add-work-order", "work_order", wo_id,
           new_values={
               "naming_series": naming, "item_id": fg_item_id,
               "bom_id": args.bom_id, "qty": str(round_currency(qty)),
           })
    conn.commit()

    ok({
        "work_order_id": wo_id,
        "naming_series": naming,
        "item_id": fg_item_id,
        "bom_id": args.bom_id,
        "qty": str(round_currency(qty)),
        "status": "draft",
        "item_count": len(bom_items),
    })


# ---------------------------------------------------------------------------
# 10. get-work-order
# ---------------------------------------------------------------------------

def get_work_order(conn, args):
    """Get work order with items and job cards.

    Required: --work-order-id
    Returns: WO dict + items list (with item names) + job cards list
    """
    if not args.work_order_id:
        err("--work-order-id is required")

    wo = conn.execute(
        "SELECT * FROM work_order WHERE id = ?", (args.work_order_id,),
    ).fetchone()
    if not wo:
        err(f"Work Order {args.work_order_id} not found")

    data = row_to_dict(wo)

    # Fetch item name for the finished good
    fg_item = conn.execute(
        "SELECT item_code, item_name FROM item WHERE id = ?",
        (data["item_id"],),
    ).fetchone()
    if fg_item:
        fg_dict = row_to_dict(fg_item)
        data["item_code"] = fg_dict.get("item_code")
        data["item_name"] = fg_dict.get("item_name")

    # Fetch BOM naming_series
    bom_row = conn.execute(
        "SELECT naming_series FROM bom WHERE id = ?", (data["bom_id"],),
    ).fetchone()
    if bom_row:
        data["bom_naming_series"] = bom_row["naming_series"]

    # Fetch work order items with item details
    items = conn.execute(
        """SELECT woi.*, i.item_code, i.item_name, i.stock_uom
           FROM work_order_item woi
           LEFT JOIN item i ON i.id = woi.item_id
           WHERE woi.work_order_id = ?
           ORDER BY woi.rowid""",
        (args.work_order_id,),
    ).fetchall()
    data["items"] = [row_to_dict(r) for r in items]

    # Fetch job cards
    job_cards = conn.execute(
        """SELECT jc.*, o.name AS operation_name,
                  w.name AS workstation_name
           FROM job_card jc
           LEFT JOIN operation o ON o.id = jc.operation_id
           LEFT JOIN workstation w ON w.id = jc.workstation_id
           WHERE jc.work_order_id = ?
           ORDER BY jc.created_at""",
        (args.work_order_id,),
    ).fetchall()
    data["job_cards"] = [row_to_dict(r) for r in job_cards]

    ok(data)


# ---------------------------------------------------------------------------
# 11. list-work-orders
# ---------------------------------------------------------------------------

def list_work_orders(conn, args):
    """Query work orders with filtering.

    Optional: --company-id, --status, --from-date, --to-date, --item-id,
              --limit (20), --offset (0)
    """
    conditions = ["1=1"]
    params = []

    if args.company_id:
        conditions.append("wo.company_id = ?")
        params.append(args.company_id)
    if args.status:
        if args.status not in VALID_WO_STATUSES:
            err(f"Invalid status '{args.status}'. Valid: {VALID_WO_STATUSES}")
        conditions.append("wo.status = ?")
        params.append(args.status)
    if args.from_date:
        conditions.append("wo.created_at >= ?")
        params.append(args.from_date)
    if args.to_date:
        conditions.append("wo.created_at <= ?")
        params.append(args.to_date + " 23:59:59")
    if args.item_id:
        conditions.append("wo.item_id = ?")
        params.append(args.item_id)

    where = " AND ".join(conditions)

    count_row = conn.execute(
        f"""SELECT COUNT(*) FROM work_order wo
            LEFT JOIN item i ON i.id = wo.item_id
            WHERE {where}""",
        params,
    ).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    params.extend([limit, offset])

    rows = conn.execute(
        f"""SELECT wo.id, wo.naming_series, wo.item_id, wo.bom_id,
               wo.qty, wo.produced_qty, wo.status, wo.company_id,
               wo.planned_start_date, wo.actual_start_date,
               wo.actual_end_date, wo.material_transferred_for_manufacturing,
               i.item_code, i.item_name
           FROM work_order wo
           LEFT JOIN item i ON i.id = wo.item_id
           WHERE {where}
           ORDER BY wo.created_at DESC
           LIMIT ? OFFSET ?""",
        params,
    ).fetchall()

    ok({"work_orders": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset, "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 12. start-work-order
# ---------------------------------------------------------------------------

def start_work_order(conn, args):
    """Start a work order (draft -> not_started).

    Required: --work-order-id
    """
    if not args.work_order_id:
        err("--work-order-id is required")

    wo = conn.execute(
        "SELECT * FROM work_order WHERE id = ?", (args.work_order_id,),
    ).fetchone()
    if not wo:
        err(f"Work Order {args.work_order_id} not found")

    wo_dict = row_to_dict(wo)
    if wo_dict["status"] != "draft":
        err(
            f"Cannot start Work Order with status '{wo_dict['status']}'. "
            f"Only 'draft' work orders can be started."
        )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """UPDATE work_order
           SET status = 'not_started', actual_start_date = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (now_str, args.work_order_id),
    )

    audit(conn, "erpclaw-manufacturing", "start-work-order", "work_order", args.work_order_id,
           old_values={"status": "draft"},
           new_values={"status": "not_started", "actual_start_date": now_str})
    conn.commit()

    ok({
        "work_order_id": args.work_order_id,
        "status": "not_started",
        "actual_start_date": now_str,
    })


# ---------------------------------------------------------------------------
# 13. transfer-materials
# ---------------------------------------------------------------------------

def transfer_materials(conn, args):
    """Transfer materials from source to WIP warehouse for a work order.

    Required: --work-order-id, --items (JSON: [{item_id, qty, warehouse_id?}])
    Optional: --posting-date (defaults to today)

    Creates SLE entries (OUT of source, IN to WIP) and perpetual GL entries
    in a single transaction.
    """
    if not args.work_order_id:
        err("--work-order-id is required")
    if not args.items:
        err("--items is required (JSON array)")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    # Validate work order
    wo = conn.execute(
        "SELECT * FROM work_order WHERE id = ?", (args.work_order_id,),
    ).fetchone()
    if not wo:
        err(f"Work Order {args.work_order_id} not found")

    wo_dict = row_to_dict(wo)
    if wo_dict["status"] not in ("not_started", "in_process"):
        err(
            f"Cannot transfer materials for Work Order with status "
            f"'{wo_dict['status']}'. Must be 'not_started' or 'in_process'."
        )

    source_wh = wo_dict.get("source_warehouse_id")
    wip_wh = wo_dict.get("wip_warehouse_id")
    if not wip_wh:
        err("Work Order has no WIP warehouse set. Set --wip-warehouse-id when creating the work order.")

    company_id = wo_dict["company_id"]
    posting_date = args.posting_date or datetime.now().strftime("%Y-%m-%d")

    # Get fiscal year for SLE
    fiscal_year = _get_fiscal_year(conn, posting_date)
    cost_center_id = _get_cost_center(conn, company_id)

    # Fetch work order items for validation
    wo_items = conn.execute(
        "SELECT * FROM work_order_item WHERE work_order_id = ?",
        (args.work_order_id,),
    ).fetchall()
    wo_item_map = {}
    for woi_row in wo_items:
        woi = row_to_dict(woi_row)
        wo_item_map[woi["item_id"]] = woi

    # Build SLE entries
    sle_entries = []
    transfer_updates = []  # (item_id, transfer_qty) tuples

    for i, item in enumerate(items):
        item_id = item.get("item_id")
        if not item_id:
            err(f"Transfer item {i}: item_id is required")

        transfer_qty = to_decimal(item.get("qty", "0"))
        if transfer_qty <= 0:
            err(f"Transfer item {i}: qty must be > 0")

        # Validate item is in the work order
        if item_id not in wo_item_map:
            err(f"Transfer item {i}: item {item_id} is not in this work order")

        woi = wo_item_map[item_id]
        required = to_decimal(woi["required_qty"])
        already_transferred = to_decimal(woi["transferred_qty"])
        if already_transferred + transfer_qty > required:
            err(
                f"Transfer item {i}: transferring {transfer_qty} would exceed "
                f"required qty. Required: {required}, already transferred: "
                f"{already_transferred}"
            )

        # Determine source warehouse for this item
        item_source_wh = item.get("warehouse_id") or woi.get("source_warehouse_id") or source_wh
        if not item_source_wh:
            err(
                f"Transfer item {i}: no source warehouse found. "
                f"Set source_warehouse_id on the work order or provide warehouse_id in items."
            )

        # Get valuation rate for this item at source warehouse
        try:
            val_rate = get_valuation_rate(conn, item_id, item_source_wh)
        except Exception:
            val_rate = Decimal("0")
        val_rate = to_decimal(str(val_rate)) if val_rate else Decimal("0")

        # SLE: OUT of source warehouse
        sle_entries.append({
            "item_id": item_id,
            "warehouse_id": item_source_wh,
            "actual_qty": str(round_currency(-transfer_qty)),
            "incoming_rate": "0",
            "fiscal_year": fiscal_year,
        })

        # SLE: IN to WIP warehouse
        sle_entries.append({
            "item_id": item_id,
            "warehouse_id": wip_wh,
            "actual_qty": str(round_currency(transfer_qty)),
            "incoming_rate": str(round_currency(val_rate)),
            "fiscal_year": fiscal_year,
        })

        transfer_updates.append((item_id, transfer_qty))

    # Insert SLE entries
    try:
        sle_ids = insert_sle_entries(
            conn, sle_entries,
            voucher_type="work_order",
            voucher_id=args.work_order_id,
            posting_date=posting_date,
            company_id=company_id,
        )
    except (ValueError, NotImplementedError) as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err(f"SLE posting failed: {e}")

    # Fetch inserted SLE rows for GL generation
    sle_rows = conn.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (args.work_order_id,),
    ).fetchall()
    sle_dicts = [row_to_dict(r) for r in sle_rows]

    # Create perpetual inventory GL entries
    gl_entries = create_perpetual_inventory_gl(
        conn, sle_dicts,
        voucher_type="work_order",
        voucher_id=args.work_order_id,
        posting_date=posting_date,
        company_id=company_id,
        cost_center_id=cost_center_id,
    )

    gl_ids = []
    if gl_entries:
        if fiscal_year:
            for gle in gl_entries:
                gle["fiscal_year"] = fiscal_year
        try:
            gl_ids = insert_gl_entries(
                conn, gl_entries,
                voucher_type="work_order",
                voucher_id=args.work_order_id,
                posting_date=posting_date,
                company_id=company_id,
                remarks=f"Material Transfer for WO {wo_dict['naming_series']}",
            )
        except (ValueError, NotImplementedError) as e:
            sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
            err(f"GL posting failed: {e}")

    # Update work_order_item transferred_qty
    total_material_transferred = Decimal("0")
    for item_id, transfer_qty in transfer_updates:
        conn.execute(
            """UPDATE work_order_item
               SET transferred_qty = CAST(
                   (transferred_qty + 0 + ?) AS TEXT)
               WHERE work_order_id = ? AND item_id = ?""",
            (str(round_currency(transfer_qty)), args.work_order_id, item_id),
        )
        total_material_transferred += transfer_qty

    # Update work_order.material_transferred_for_manufacturing
    current_transferred = to_decimal(wo_dict["material_transferred_for_manufacturing"])
    new_transferred = round_currency(current_transferred + total_material_transferred)
    conn.execute(
        """UPDATE work_order
           SET material_transferred_for_manufacturing = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (str(new_transferred), args.work_order_id),
    )

    # Transition to in_process if currently not_started
    if wo_dict["status"] == "not_started":
        conn.execute(
            "UPDATE work_order SET status = 'in_process', updated_at = datetime('now') WHERE id = ?",
            (args.work_order_id,),
        )

    audit(conn, "erpclaw-manufacturing", "transfer-materials", "work_order", args.work_order_id,
           new_values={
               "items_transferred": len(transfer_updates),
               "sle_count": len(sle_ids) if sle_ids else 0,
               "gl_count": len(gl_ids),
           })
    conn.commit()

    ok({
        "work_order_id": args.work_order_id,
        "items_transferred": len(transfer_updates),
        "sle_count": len(sle_ids) if sle_ids else 0,
        "gl_count": len(gl_ids),
    })


# ---------------------------------------------------------------------------
# 14. create-job-card
# ---------------------------------------------------------------------------

def create_job_card(conn, args):
    """Create a Job Card for a work order operation.

    Required: --work-order-id, --operation-id
    Optional: --workstation-id, --for-quantity
    """
    if not args.work_order_id:
        err("--work-order-id is required")
    if not args.operation_id:
        err("--operation-id is required")

    # Validate work order
    wo = conn.execute(
        "SELECT * FROM work_order WHERE id = ?", (args.work_order_id,),
    ).fetchone()
    if not wo:
        err(f"Work Order {args.work_order_id} not found")

    wo_dict = row_to_dict(wo)
    if wo_dict["status"] not in ("not_started", "in_process"):
        err(
            f"Cannot create Job Card for Work Order with status "
            f"'{wo_dict['status']}'. Must be 'not_started' or 'in_process'."
        )

    # Validate operation
    op = _validate_operation_exists(conn, args.operation_id)
    op_dict = row_to_dict(op)

    # Determine workstation
    ws_id = args.workstation_id
    if not ws_id:
        ws_id = op_dict.get("default_workstation_id")
    if ws_id:
        _validate_workstation_exists(conn, ws_id)

    # For quantity defaults to WO qty
    for_qty = str(round_currency(
        to_decimal(args.for_quantity or wo_dict["qty"])
    ))

    # Generate naming series
    naming = get_next_name(conn, "job_card", company_id=wo_dict["company_id"])

    jc_id = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO job_card
               (id, naming_series, work_order_id, operation_id, workstation_id,
                for_quantity, completed_qty, total_time_in_minutes, status)
               VALUES (?, ?, ?, ?, ?, ?, '0', '0', 'open')""",
            (jc_id, naming, args.work_order_id, args.operation_id,
             ws_id, for_qty),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("Job Card creation failed — check for duplicates or invalid data")

    audit(conn, "erpclaw-manufacturing", "create-job-card", "job_card", jc_id,
           new_values={
               "naming_series": naming,
               "work_order_id": args.work_order_id,
               "operation_id": args.operation_id,
               "for_quantity": for_qty,
           })
    conn.commit()

    ok({
        "job_card_id": jc_id,
        "naming_series": naming,
        "work_order_id": args.work_order_id,
        "operation_id": args.operation_id,
        "workstation_id": ws_id,
        "for_quantity": for_qty,
        "status": "open",
    })


# ---------------------------------------------------------------------------
# 15. complete-job-card
# ---------------------------------------------------------------------------

def complete_job_card(conn, args):
    """Complete a Job Card with time recording.

    Required: --job-card-id, --actual-time-in-mins
    Optional: --completed-qty
    """
    if not args.job_card_id:
        err("--job-card-id is required")
    if not args.actual_time_in_mins:
        err("--actual-time-in-mins is required")

    jc = conn.execute(
        "SELECT * FROM job_card WHERE id = ?", (args.job_card_id,),
    ).fetchone()
    if not jc:
        err(f"Job Card {args.job_card_id} not found")

    jc_dict = row_to_dict(jc)
    if jc_dict["status"] not in ("open", "in_process"):
        err(
            f"Cannot complete Job Card with status '{jc_dict['status']}'. "
            f"Must be 'open' or 'in_process'."
        )

    time_mins = str(round_currency(to_decimal(args.actual_time_in_mins)))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    updates = {
        "status": "completed",
        "total_time_in_minutes": time_mins,
        "time_completed": now_str,
    }

    completed_qty = None
    if args.completed_qty:
        completed_qty = str(round_currency(to_decimal(args.completed_qty)))
        updates["completed_qty"] = completed_qty

    if completed_qty:
        conn.execute(
            """UPDATE job_card
               SET status = 'completed', total_time_in_minutes = ?,
                   time_completed = ?, completed_qty = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (time_mins, now_str, completed_qty, args.job_card_id),
        )
    else:
        conn.execute(
            """UPDATE job_card
               SET status = 'completed', total_time_in_minutes = ?,
                   time_completed = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (time_mins, now_str, args.job_card_id),
        )

    audit(conn, "erpclaw-manufacturing", "complete-job-card", "job_card", args.job_card_id,
           old_values={"status": jc_dict["status"]},
           new_values=updates)
    conn.commit()

    ok({
        "job_card_id": args.job_card_id,
        "status": "completed",
        "total_time_in_minutes": time_mins,
        "time_completed": now_str,
        "completed_qty": completed_qty or jc_dict.get("completed_qty", "0"),
    })


# ---------------------------------------------------------------------------
# 16. complete-work-order
# ---------------------------------------------------------------------------

def complete_work_order(conn, args):
    """Complete a work order, producing finished goods.

    Required: --work-order-id
    Optional: --produced-qty (defaults to WO qty), --posting-date

    Calculates production cost (RM + operating), creates SLE for FG receipt
    into target warehouse, and posts perpetual GL entries.
    """
    if not args.work_order_id:
        err("--work-order-id is required")

    wo = conn.execute(
        "SELECT * FROM work_order WHERE id = ?", (args.work_order_id,),
    ).fetchone()
    if not wo:
        err(f"Work Order {args.work_order_id} not found")

    wo_dict = row_to_dict(wo)
    if wo_dict["status"] != "in_process":
        err(
            f"Cannot complete Work Order with status '{wo_dict['status']}'. "
            f"Must be 'in_process'."
        )

    produced_qty = to_decimal(args.produced_qty or wo_dict["qty"])
    if produced_qty <= 0:
        err("--produced-qty must be greater than 0")

    target_wh = wo_dict.get("target_warehouse_id")
    if not target_wh:
        err("Work Order has no target warehouse. Set --target-warehouse-id when creating.")

    wip_wh = wo_dict.get("wip_warehouse_id")
    company_id = wo_dict["company_id"]
    fg_item_id = wo_dict["item_id"]
    posting_date = args.posting_date or datetime.now().strftime("%Y-%m-%d")

    fiscal_year = _get_fiscal_year(conn, posting_date)
    cost_center_id = _get_cost_center(conn, company_id)

    # --- Calculate production cost ---

    # 1. Raw Material cost: sum of (transferred_qty * valuation_rate) per WO item
    wo_items = conn.execute(
        "SELECT * FROM work_order_item WHERE work_order_id = ?",
        (args.work_order_id,),
    ).fetchall()

    rm_cost = Decimal("0")
    for woi_row in wo_items:
        woi = row_to_dict(woi_row)
        transferred = to_decimal(woi["transferred_qty"])
        if transferred <= 0:
            continue
        # Get valuation rate from SLE for the item at WIP warehouse
        try:
            val_rate = get_valuation_rate(conn, woi["item_id"], wip_wh)
            val_rate = to_decimal(str(val_rate)) if val_rate else Decimal("0")
        except Exception:
            val_rate = Decimal("0")
        rm_cost += round_currency(transferred * val_rate)
    rm_cost = round_currency(rm_cost)

    # 2. Operating cost: sum from completed job cards
    job_cards = conn.execute(
        """SELECT jc.total_time_in_minutes, jc.workstation_id
           FROM job_card jc
           WHERE jc.work_order_id = ? AND jc.status = 'completed'""",
        (args.work_order_id,),
    ).fetchall()

    operating_cost = Decimal("0")
    for jc_row in job_cards:
        jc = row_to_dict(jc_row)
        time_mins = to_decimal(jc.get("total_time_in_minutes", "0"))
        ws_id = jc.get("workstation_id")
        if ws_id and time_mins > 0:
            ws = conn.execute(
                "SELECT operating_cost_per_hour FROM workstation WHERE id = ?",
                (ws_id,),
            ).fetchone()
            if ws:
                hour_rate = to_decimal(ws["operating_cost_per_hour"])
                operating_cost += round_currency(
                    (time_mins / Decimal("60")) * hour_rate
                )
    operating_cost = round_currency(operating_cost)

    # 3. FG production rate
    total_production_cost = round_currency(rm_cost + operating_cost)
    fg_rate = round_currency(total_production_cost / produced_qty) if produced_qty > 0 else Decimal("0")

    # Build SLE entries: FG item INTO target warehouse
    sle_entries = [{
        "item_id": fg_item_id,
        "warehouse_id": target_wh,
        "actual_qty": str(round_currency(produced_qty)),
        "incoming_rate": str(fg_rate),
        "fiscal_year": fiscal_year,
    }]

    # Consume raw materials from WIP warehouse (negative SLE entries)
    if wip_wh:
        for woi_row in wo_items:
            woi = row_to_dict(woi_row)
            transferred = to_decimal(woi["transferred_qty"])
            if transferred <= 0:
                continue
            sle_entries.append({
                "item_id": woi["item_id"],
                "warehouse_id": wip_wh,
                "actual_qty": str(round_currency(-transferred)),
                "incoming_rate": "0",
                "fiscal_year": fiscal_year,
            })

    # We use a unique voucher_id suffix to separate material transfer SLEs
    # from completion SLEs for the same work order.
    completion_voucher_id = f"{args.work_order_id}:completion"

    try:
        sle_ids = insert_sle_entries(
            conn, sle_entries,
            voucher_type="work_order",
            voucher_id=completion_voucher_id,
            posting_date=posting_date,
            company_id=company_id,
        )
    except (ValueError, NotImplementedError) as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err(f"SLE posting failed: {e}")

    # Fetch SLE rows for GL generation
    sle_rows = conn.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (completion_voucher_id,),
    ).fetchall()
    sle_dicts = [row_to_dict(r) for r in sle_rows]

    # Create perpetual inventory GL entries
    gl_entries = create_perpetual_inventory_gl(
        conn, sle_dicts,
        voucher_type="work_order",
        voucher_id=completion_voucher_id,
        posting_date=posting_date,
        company_id=company_id,
        cost_center_id=cost_center_id,
    )

    gl_ids = []
    if gl_entries:
        if fiscal_year:
            for gle in gl_entries:
                gle["fiscal_year"] = fiscal_year
        try:
            gl_ids = insert_gl_entries(
                conn, gl_entries,
                voucher_type="work_order",
                voucher_id=completion_voucher_id,
                posting_date=posting_date,
                company_id=company_id,
                remarks=f"Production Completion for WO {wo_dict['naming_series']}",
            )
        except (ValueError, NotImplementedError) as e:
            sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
            err(f"GL posting failed: {e}")

    # Update work order
    conn.execute(
        """UPDATE work_order
           SET produced_qty = ?, status = 'completed',
               actual_end_date = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (str(round_currency(produced_qty)),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         args.work_order_id),
    )

    # Mark all work order items as fully consumed
    conn.execute(
        """UPDATE work_order_item
           SET consumed_qty = transferred_qty
           WHERE work_order_id = ?""",
        (args.work_order_id,),
    )

    audit(conn, "erpclaw-manufacturing", "complete-work-order", "work_order", args.work_order_id,
           new_values={
               "status": "completed",
               "produced_qty": str(round_currency(produced_qty)),
               "rm_cost": str(rm_cost),
               "operating_cost": str(operating_cost),
               "total_production_cost": str(total_production_cost),
               "fg_rate": str(fg_rate),
           })
    conn.commit()

    ok({
        "work_order_id": args.work_order_id,
        "status": "completed",
        "produced_qty": str(round_currency(produced_qty)),
        "rm_cost": str(rm_cost),
        "operating_cost": str(operating_cost),
        "production_cost": str(total_production_cost),
        "fg_rate": str(fg_rate),
        "sle_count": len(sle_ids) if sle_ids else 0,
        "gl_count": len(gl_ids),
    })


# ---------------------------------------------------------------------------
# 17. cancel-work-order
# ---------------------------------------------------------------------------

def cancel_work_order(conn, args):
    """Cancel a work order, reversing all SLE and GL entries.

    Required: --work-order-id
    """
    if not args.work_order_id:
        err("--work-order-id is required")

    wo = conn.execute(
        "SELECT * FROM work_order WHERE id = ?", (args.work_order_id,),
    ).fetchone()
    if not wo:
        err(f"Work Order {args.work_order_id} not found")

    wo_dict = row_to_dict(wo)
    if wo_dict["status"] in ("completed", "cancelled"):
        err(
            f"Cannot cancel Work Order with status '{wo_dict['status']}'. "
            f"Completed and cancelled work orders cannot be cancelled."
        )

    posting_date = args.posting_date or datetime.now().strftime("%Y-%m-%d")

    # Reverse material transfer SLE entries
    try:
        reverse_sle_entries(
            conn,
            voucher_type="work_order",
            voucher_id=args.work_order_id,
            posting_date=posting_date,
        )
    except (ValueError, NotImplementedError):
        pass  # No SLE entries to reverse is acceptable

    # Reverse material transfer GL entries
    try:
        reverse_gl_entries(
            conn,
            voucher_type="work_order",
            voucher_id=args.work_order_id,
            posting_date=posting_date,
        )
    except ValueError:
        pass  # No GL entries to reverse is acceptable

    # Also reverse completion entries if any exist
    completion_voucher_id = f"{args.work_order_id}:completion"
    try:
        reverse_sle_entries(
            conn,
            voucher_type="work_order",
            voucher_id=completion_voucher_id,
            posting_date=posting_date,
        )
    except (ValueError, NotImplementedError):
        pass

    try:
        reverse_gl_entries(
            conn,
            voucher_type="work_order",
            voucher_id=completion_voucher_id,
            posting_date=posting_date,
        )
    except ValueError:
        pass

    # Set WO status to cancelled
    conn.execute(
        """UPDATE work_order
           SET status = 'cancelled', updated_at = datetime('now')
           WHERE id = ?""",
        (args.work_order_id,),
    )

    # Cancel all open/in_process job cards for this WO
    conn.execute(
        """UPDATE job_card
           SET status = 'cancelled', updated_at = datetime('now')
           WHERE work_order_id = ? AND status IN ('open', 'in_process')""",
        (args.work_order_id,),
    )

    audit(conn, "erpclaw-manufacturing", "cancel-work-order", "work_order", args.work_order_id,
           old_values={"status": wo_dict["status"]},
           new_values={"status": "cancelled"})
    conn.commit()

    ok({
        "work_order_id": args.work_order_id,
        "status": "cancelled",
    })


# ---------------------------------------------------------------------------
# 18. create-production-plan
# ---------------------------------------------------------------------------

def create_production_plan(conn, args):
    """Create a Production Plan.

    Required: --company-id, --items (JSON: [{item_id, bom_id, planned_qty,
              warehouse_id?, sales_order_id?}])
    Optional: --planning-horizon-days (default 30)
    """
    if not args.company_id:
        err("--company-id is required")
    if not args.items:
        err("--items is required (JSON array)")

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    _validate_company_exists(conn, args.company_id)

    # Planning horizon
    horizon_days = int(args.planning_horizon_days or "30")
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    end_date = (datetime.now() + timedelta(days=horizon_days)).strftime("%Y-%m-%d")

    # Generate naming series
    naming = get_next_name(conn, "production_plan", company_id=args.company_id)

    plan_id = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO production_plan
               (id, naming_series, planning_period_start, planning_period_end,
                status, company_id)
               VALUES (?, ?, ?, ?, 'draft', ?)""",
            (plan_id, naming, today, end_date, args.company_id),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("Production Plan creation failed — check for duplicates or invalid data")

    # Insert plan items
    plan_item_count = 0
    for i, item in enumerate(items):
        item_id = item.get("item_id")
        bom_id = item.get("bom_id")
        planned_qty = item.get("planned_qty")

        if not item_id:
            err(f"Plan item {i}: item_id is required")
        if not bom_id:
            err(f"Plan item {i}: bom_id is required")
        if not planned_qty:
            err(f"Plan item {i}: planned_qty is required")

        _validate_item_exists(conn, item_id, f"Plan item {i}: item")
        _validate_bom_exists(conn, bom_id)

        qty = to_decimal(planned_qty)
        if qty <= 0:
            err(f"Plan item {i}: planned_qty must be > 0")

        warehouse_id = item.get("warehouse_id")
        if warehouse_id:
            _validate_warehouse_exists(conn, warehouse_id, f"Plan item {i}: warehouse")

        conn.execute(
            """INSERT INTO production_plan_item
               (id, production_plan_id, item_id, bom_id, planned_qty,
                produced_qty, ordered_qty, sales_order_id, warehouse_id)
               VALUES (?, ?, ?, ?, ?, '0', '0', ?, ?)""",
            (str(uuid.uuid4()), plan_id, item_id, bom_id,
             str(round_currency(qty)),
             item.get("sales_order_id"), warehouse_id),
        )
        plan_item_count += 1

    audit(conn, "erpclaw-manufacturing", "create-production-plan", "production_plan", plan_id,
           new_values={
               "naming_series": naming,
               "company_id": args.company_id,
               "item_count": plan_item_count,
               "horizon_days": horizon_days,
           })
    conn.commit()

    ok({
        "production_plan_id": plan_id,
        "naming_series": naming,
        "planning_period_start": today,
        "planning_period_end": end_date,
        "item_count": plan_item_count,
        "status": "draft",
    })


# ---------------------------------------------------------------------------
# MRP helper: BOM explosion for material planning
# ---------------------------------------------------------------------------

def _explode_bom_for_mrp(conn, bom_id, parent_qty):
    """Explode a BOM recursively and return flat list of leaf raw materials.

    Returns dict: {item_id: {"item_id", "total_qty"}}
    Reuses the same recursive logic as explode_bom action but as a standalone
    helper for MRP and other internal callers.
    """
    visited = set()
    leaf_materials = {}

    def _recurse(current_bom_id, qty, depth):
        if depth > MAX_BOM_EXPLOSION_DEPTH:
            return
        if current_bom_id in visited:
            return

        visited.add(current_bom_id)

        current_bom = conn.execute(
            "SELECT quantity FROM bom WHERE id = ?", (current_bom_id,),
        ).fetchone()
        if not current_bom:
            visited.discard(current_bom_id)
            return

        bom_base_qty = to_decimal(current_bom["quantity"])
        if bom_base_qty <= 0:
            bom_base_qty = Decimal("1")

        bom_items = conn.execute(
            "SELECT * FROM bom_item WHERE bom_id = ?", (current_bom_id,),
        ).fetchall()

        for bi_row in bom_items:
            bi = row_to_dict(bi_row)
            item_qty = to_decimal(bi["quantity"])
            scaled_qty = round_currency((item_qty / bom_base_qty) * qty)

            scrap_pct = to_decimal(bi.get("scrap_percentage", "0"))
            if scrap_pct > 0:
                scaled_qty = round_currency(
                    scaled_qty * (Decimal("1") + scrap_pct / Decimal("100"))
                )

            if bi.get("is_sub_assembly") and bi.get("sub_bom_id"):
                _recurse(bi["sub_bom_id"], scaled_qty, depth + 1)
            else:
                rm_id = bi["item_id"]
                if rm_id in leaf_materials:
                    leaf_materials[rm_id]["total_qty"] = round_currency(
                        to_decimal(leaf_materials[rm_id]["total_qty"]) + scaled_qty
                    )
                else:
                    leaf_materials[rm_id] = {
                        "item_id": rm_id,
                        "total_qty": round_currency(scaled_qty),
                    }

        visited.discard(current_bom_id)

    _recurse(bom_id, parent_qty, depth=0)
    return leaf_materials


# ---------------------------------------------------------------------------
# 19. run-mrp
# ---------------------------------------------------------------------------

def run_mrp(conn, args):
    """Run Material Requirements Planning for a production plan.

    Required: --production-plan-id

    For each plan item, explodes its BOM to leaf materials, computes
    available stock and on-order quantities, and calculates shortfalls.
    """
    if not args.production_plan_id:
        err("--production-plan-id is required")

    plan = conn.execute(
        "SELECT * FROM production_plan WHERE id = ?",
        (args.production_plan_id,),
    ).fetchone()
    if not plan:
        err(f"Production Plan {args.production_plan_id} not found")

    plan_dict = row_to_dict(plan)
    if plan_dict["status"] != "draft":
        err(
            f"Cannot run MRP on plan with status '{plan_dict['status']}'. "
            f"Plan must be in 'draft' status."
        )

    company_id = plan_dict["company_id"]

    # Delete any existing materials from a previous MRP run
    conn.execute(
        "DELETE FROM production_plan_material WHERE production_plan_id = ?",
        (args.production_plan_id,),
    )

    # Fetch plan items
    plan_items = conn.execute(
        "SELECT * FROM production_plan_item WHERE production_plan_id = ?",
        (args.production_plan_id,),
    ).fetchall()

    if not plan_items:
        err("Production Plan has no items")

    # Accumulate total required materials across all plan items
    # {item_id: {"total_qty": Decimal, "warehouse_id": str or None}}
    aggregated_materials = {}

    for pi_row in plan_items:
        pi = row_to_dict(pi_row)
        bom_id = pi["bom_id"]
        planned_qty = to_decimal(pi["planned_qty"])
        warehouse_id = pi.get("warehouse_id")

        # Explode BOM to leaf materials
        leaf_materials = _explode_bom_for_mrp(conn, bom_id, planned_qty)

        for item_id, mat_info in leaf_materials.items():
            mat_qty = to_decimal(mat_info["total_qty"])
            key = (item_id, warehouse_id or "")
            if key in aggregated_materials:
                aggregated_materials[key]["total_qty"] = round_currency(
                    to_decimal(aggregated_materials[key]["total_qty"]) + mat_qty
                )
            else:
                aggregated_materials[key] = {
                    "item_id": item_id,
                    "total_qty": round_currency(mat_qty),
                    "warehouse_id": warehouse_id,
                }

    # For each aggregated material, compute availability and shortfall
    material_count = 0
    total_shortfall_items = 0

    for key, mat in aggregated_materials.items():
        item_id = mat["item_id"]
        warehouse_id = mat["warehouse_id"]
        required_qty = to_decimal(mat["total_qty"])

        # Get available stock
        available_qty = Decimal("0")
        if warehouse_id:
            try:
                bal = get_stock_balance(conn, item_id, warehouse_id)
                available_qty = to_decimal(bal["qty"]) if bal else Decimal("0")
            except (ValueError, KeyError, NotImplementedError):
                available_qty = Decimal("0")
        else:
            # If no warehouse specified, sum across all warehouses for company
            try:
                stock_rows = conn.execute(
                    """SELECT decimal_sum(actual_qty) AS total_qty
                       FROM stock_ledger_entry
                       WHERE item_id = ? AND is_cancelled = 0""",
                    (item_id,),
                ).fetchone()
                if stock_rows and stock_rows["total_qty"]:
                    available_qty = to_decimal(str(stock_rows["total_qty"]))
            except Exception:
                available_qty = Decimal("0")

        # Get on-order quantity from open purchase orders
        on_order_qty = Decimal("0")
        try:
            po_row = conn.execute(
                """SELECT COALESCE(decimal_sum(poi.qty - poi.received_qty), '0') AS pending
                   FROM purchase_order_item poi
                   JOIN purchase_order po ON po.id = poi.purchase_order_id
                   WHERE poi.item_id = ?
                     AND po.status NOT IN ('cancelled', 'closed')
                     AND poi.qty + 0 > poi.received_qty + 0""",
                (item_id,),
            ).fetchone()
            if po_row and po_row["pending"]:
                on_order_qty = to_decimal(str(po_row["pending"]))
        except Exception:
            on_order_qty = Decimal("0")

        # Calculate shortfall
        available_qty = round_currency(available_qty)
        on_order_qty = round_currency(on_order_qty)
        shortfall_qty = round_currency(
            max(Decimal("0"), required_qty - available_qty - on_order_qty)
        )

        conn.execute(
            """INSERT INTO production_plan_material
               (id, production_plan_id, item_id, required_qty,
                available_qty, on_order_qty, shortfall_qty, warehouse_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), args.production_plan_id, item_id,
             str(round_currency(required_qty)),
             str(available_qty), str(on_order_qty),
             str(shortfall_qty), warehouse_id),
        )
        material_count += 1
        if shortfall_qty > 0:
            total_shortfall_items += 1

    # Update plan status
    conn.execute(
        """UPDATE production_plan
           SET status = 'submitted', updated_at = datetime('now')
           WHERE id = ?""",
        (args.production_plan_id,),
    )

    audit(conn, "erpclaw-manufacturing", "run-mrp", "production_plan", args.production_plan_id,
           new_values={
               "material_count": material_count,
               "total_shortfall_items": total_shortfall_items,
           })
    conn.commit()

    ok({
        "production_plan_id": args.production_plan_id,
        "status": "submitted",
        "material_count": material_count,
        "total_shortfall_items": total_shortfall_items,
    })


# ---------------------------------------------------------------------------
# 20. get-production-plan
# ---------------------------------------------------------------------------

def get_production_plan(conn, args):
    """Get a production plan with items and materials.

    Required: --production-plan-id
    Returns: plan dict + items list (with item/BOM names) + materials list
    """
    if not args.production_plan_id:
        err("--production-plan-id is required")

    plan = conn.execute(
        "SELECT * FROM production_plan WHERE id = ?",
        (args.production_plan_id,),
    ).fetchone()
    if not plan:
        err(f"Production Plan {args.production_plan_id} not found")

    data = row_to_dict(plan)

    # Fetch plan items with item and BOM details
    items = conn.execute(
        """SELECT ppi.*, i.item_code, i.item_name,
                  b.naming_series AS bom_naming_series
           FROM production_plan_item ppi
           LEFT JOIN item i ON i.id = ppi.item_id
           LEFT JOIN bom b ON b.id = ppi.bom_id
           WHERE ppi.production_plan_id = ?
           ORDER BY ppi.rowid""",
        (args.production_plan_id,),
    ).fetchall()
    data["items"] = [row_to_dict(r) for r in items]

    # Fetch materials with item details
    materials = conn.execute(
        """SELECT ppm.*, i.item_code, i.item_name, i.stock_uom
           FROM production_plan_material ppm
           LEFT JOIN item i ON i.id = ppm.item_id
           WHERE ppm.production_plan_id = ?
           ORDER BY ppm.rowid""",
        (args.production_plan_id,),
    ).fetchall()
    data["materials"] = [row_to_dict(r) for r in materials]

    # Summary
    total_shortfall = sum(
        1 for m in data["materials"]
        if to_decimal(m.get("shortfall_qty", "0")) > 0
    )
    data["total_shortfall_items"] = total_shortfall

    ok(data)


# ---------------------------------------------------------------------------
# 21. generate-work-orders
# ---------------------------------------------------------------------------

def generate_work_orders(conn, args):
    """Generate work orders from a production plan.

    Required: --production-plan-id

    Creates a work order for each plan item that does not yet have one.
    """
    if not args.production_plan_id:
        err("--production-plan-id is required")

    plan = conn.execute(
        "SELECT * FROM production_plan WHERE id = ?",
        (args.production_plan_id,),
    ).fetchone()
    if not plan:
        err(f"Production Plan {args.production_plan_id} not found")

    plan_dict = row_to_dict(plan)
    company_id = plan_dict["company_id"]

    # Fetch plan items without work orders
    plan_items = conn.execute(
        """SELECT * FROM production_plan_item
           WHERE production_plan_id = ? AND work_order_id IS NULL""",
        (args.production_plan_id,),
    ).fetchall()

    if not plan_items:
        ok({
            "production_plan_id": args.production_plan_id,
            "work_orders_created": 0,
            "message": "All plan items already have work orders",
        })
        return  # _ok exits, but guard anyway

    work_orders_created = 0
    created_wo_ids = []

    for pi_row in plan_items:
        pi = row_to_dict(pi_row)
        bom_id = pi["bom_id"]
        planned_qty = to_decimal(pi["planned_qty"])
        warehouse_id = pi.get("warehouse_id")

        # Get BOM details
        bom = conn.execute(
            "SELECT * FROM bom WHERE id = ?", (bom_id,),
        ).fetchone()
        if not bom:
            continue
        bom_dict = row_to_dict(bom)
        fg_item_id = bom_dict["item_id"]
        bom_base_qty = to_decimal(bom_dict["quantity"])
        if bom_base_qty <= 0:
            bom_base_qty = Decimal("1")

        # Generate naming series for WO
        naming = get_next_name(conn, "work_order", company_id=company_id)

        wo_id = str(uuid.uuid4())
        try:
            conn.execute(
                """INSERT INTO work_order
                   (id, naming_series, item_id, bom_id, qty, produced_qty,
                    production_plan_id, sales_order_id,
                    target_warehouse_id,
                    status, material_transferred_for_manufacturing, company_id)
                   VALUES (?, ?, ?, ?, ?, '0', ?, ?, ?, 'draft', '0', ?)""",
                (wo_id, naming, fg_item_id, bom_id,
                 str(round_currency(planned_qty)),
                 args.production_plan_id, pi.get("sales_order_id"),
                 warehouse_id, company_id),
            )
        except sqlite3.IntegrityError:
            continue

        # Copy BOM items to work_order_item
        bom_items = conn.execute(
            "SELECT item_id, quantity, source_warehouse_id FROM bom_item WHERE bom_id = ?",
            (bom_id,),
        ).fetchall()

        for bi_row in bom_items:
            bi = row_to_dict(bi_row)
            bi_qty = to_decimal(bi["quantity"])
            required_qty = round_currency((bi_qty / bom_base_qty) * planned_qty)

            conn.execute(
                """INSERT INTO work_order_item
                   (id, work_order_id, item_id, required_qty,
                    transferred_qty, consumed_qty, source_warehouse_id)
                   VALUES (?, ?, ?, ?, '0', '0', ?)""",
                (str(uuid.uuid4()), wo_id, bi["item_id"],
                 str(required_qty), bi.get("source_warehouse_id")),
            )

        # Link work order back to plan item
        conn.execute(
            "UPDATE production_plan_item SET work_order_id = ? WHERE id = ?",
            (wo_id, pi["id"]),
        )

        audit(conn, "erpclaw-manufacturing", "generate-work-orders", "work_order", wo_id,
               new_values={
                   "naming_series": naming,
                   "production_plan_id": args.production_plan_id,
                   "item_id": fg_item_id,
                   "qty": str(round_currency(planned_qty)),
               })

        created_wo_ids.append(wo_id)
        work_orders_created += 1

    conn.commit()

    ok({
        "production_plan_id": args.production_plan_id,
        "work_orders_created": work_orders_created,
        "work_order_ids": created_wo_ids,
    })


# ---------------------------------------------------------------------------
# 22. generate-purchase-requests
# ---------------------------------------------------------------------------

def generate_purchase_requests(conn, args):
    """Generate purchase request data from a production plan's MRP results.

    Required: --production-plan-id

    Returns the list of materials with shortfalls. For v1, this returns
    data for the AI agent to relay to the buying skill.
    """
    if not args.production_plan_id:
        err("--production-plan-id is required")

    plan = conn.execute(
        "SELECT * FROM production_plan WHERE id = ?",
        (args.production_plan_id,),
    ).fetchone()
    if not plan:
        err(f"Production Plan {args.production_plan_id} not found")

    # Fetch all materials with shortfall > 0
    materials = conn.execute(
        """SELECT ppm.*, i.item_code, i.item_name, i.stock_uom
           FROM production_plan_material ppm
           LEFT JOIN item i ON i.id = ppm.item_id
           WHERE ppm.production_plan_id = ?
             AND ppm.shortfall_qty + 0 > 0
           ORDER BY ppm.rowid""",
        (args.production_plan_id,),
    ).fetchall()

    purchase_requests = []
    for mat_row in materials:
        mat = row_to_dict(mat_row)
        purchase_requests.append({
            "item_id": mat["item_id"],
            "item_code": mat.get("item_code", ""),
            "item_name": mat.get("item_name", ""),
            "uom": mat.get("stock_uom", ""),
            "required_qty": mat["required_qty"],
            "available_qty": mat["available_qty"],
            "on_order_qty": mat["on_order_qty"],
            "shortfall_qty": mat["shortfall_qty"],
            "warehouse_id": mat.get("warehouse_id"),
        })

    audit(conn, "erpclaw-manufacturing", "generate-purchase-requests", "production_plan",
           args.production_plan_id,
           new_values={"shortfall_items": len(purchase_requests)})
    conn.commit()

    ok({
        "production_plan_id": args.production_plan_id,
        "purchase_requests": purchase_requests,
        "shortfall_item_count": len(purchase_requests),
    })


# ---------------------------------------------------------------------------
# 23. add-subcontracting-order
# ---------------------------------------------------------------------------

def add_subcontracting_order(conn, args):
    """Create a subcontracting order.

    Required: --supplier-id, --bom-id, --quantity, --company-id
    Optional: --service-item-id, --supplier-warehouse-id
    """
    if not args.supplier_id:
        err("--supplier-id is required")
    if not args.bom_id:
        err("--bom-id is required")
    if not args.quantity:
        err("--quantity is required")
    if not args.company_id:
        err("--company-id is required")

    qty = to_decimal(args.quantity)
    if qty <= 0:
        err("--quantity must be greater than 0")

    # Validate supplier
    supplier = conn.execute(
        "SELECT id FROM supplier WHERE id = ? OR name = ?",
        (args.supplier_id, args.supplier_id),
    ).fetchone()
    if not supplier:
        err(f"Supplier {args.supplier_id} not found")
    args.supplier_id = supplier["id"]

    # Validate BOM
    bom = _validate_bom_exists(conn, args.bom_id)
    bom_dict = row_to_dict(bom)
    finished_item_id = bom_dict["item_id"]

    # Validate company
    _validate_company_exists(conn, args.company_id)

    # Service item: required by schema (NOT NULL), use provided or FG item as fallback
    service_item_id = args.service_item_id or finished_item_id
    _validate_item_exists(conn, service_item_id, "Service item")

    # Validate supplier warehouse if provided
    supplier_wh = args.supplier_warehouse_id
    if supplier_wh:
        _validate_warehouse_exists(conn, supplier_wh, "Supplier warehouse")

    # Generate naming series
    naming = get_next_name(conn, "subcontracting_order", company_id=args.company_id)

    sco_id = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO subcontracting_order
               (id, naming_series, supplier_id, service_item_id,
                finished_item_id, bom_id, qty,
                supplier_warehouse_id, status,
                materials_transferred, received_qty, company_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', '0', '0', ?)""",
            (sco_id, naming, args.supplier_id, service_item_id,
             finished_item_id, args.bom_id, str(round_currency(qty)),
             supplier_wh, args.company_id),
        )
    except sqlite3.IntegrityError as e:
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("Subcontracting order creation failed — check for duplicates or invalid data")

    audit(conn, "erpclaw-manufacturing", "add-subcontracting-order", "subcontracting_order", sco_id,
           new_values={
               "naming_series": naming,
               "supplier_id": args.supplier_id,
               "bom_id": args.bom_id,
               "finished_item_id": finished_item_id,
               "qty": str(round_currency(qty)),
           })
    conn.commit()

    ok({
        "subcontracting_order_id": sco_id,
        "naming_series": naming,
        "supplier_id": args.supplier_id,
        "finished_item_id": finished_item_id,
        "bom_id": args.bom_id,
        "qty": str(round_currency(qty)),
        "status": "draft",
    })


# ---------------------------------------------------------------------------
# 24. status
# ---------------------------------------------------------------------------

def status_action(conn, args):
    """Get manufacturing module status summary.

    Optional: --company-id (filter by company)
    """
    company_filter = ""
    params = []
    if args.company_id:
        company_filter = "AND company_id = ?"
        params = [args.company_id]

    # BOMs
    total_boms = conn.execute(
        f"SELECT COUNT(*) FROM bom WHERE 1=1 {company_filter}", params,
    ).fetchone()[0]
    active_boms = conn.execute(
        f"SELECT COUNT(*) FROM bom WHERE is_active = 1 {company_filter}", params,
    ).fetchone()[0]

    # Work Orders by status
    wo_statuses = {}
    for status in VALID_WO_STATUSES:
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM work_order WHERE status = ? {company_filter}",
            [status] + params,
        ).fetchone()[0]
        if cnt > 0:
            wo_statuses[status] = cnt

    total_wos = conn.execute(
        f"SELECT COUNT(*) FROM work_order WHERE 1=1 {company_filter}", params,
    ).fetchone()[0]

    # Job Cards
    open_job_cards = conn.execute(
        """SELECT COUNT(*) FROM job_card jc
           JOIN work_order wo ON wo.id = jc.work_order_id
           WHERE jc.status IN ('open', 'in_process')"""
        + (f" AND wo.company_id = ?" if args.company_id else ""),
        params,
    ).fetchone()[0]

    # Production Plans
    active_plans = conn.execute(
        f"""SELECT COUNT(*) FROM production_plan
            WHERE status NOT IN ('cancelled')
            {company_filter}""",
        params,
    ).fetchone()[0]

    # Subcontracting Orders
    active_scos = conn.execute(
        f"""SELECT COUNT(*) FROM subcontracting_order
            WHERE status NOT IN ('cancelled', 'completed')
            {company_filter}""",
        params,
    ).fetchone()[0]

    # Operations and Workstations
    total_operations = conn.execute(
        "SELECT COUNT(*) FROM operation WHERE is_active = 1",
    ).fetchone()[0]
    total_workstations = conn.execute(
        "SELECT COUNT(*) FROM workstation WHERE status = 'active'",
    ).fetchone()[0]

    ok({
        "total_boms": total_boms,
        "active_boms": active_boms,
        "total_work_orders": total_wos,
        "work_orders_by_status": wo_statuses,
        "open_job_cards": open_job_cards,
        "active_production_plans": active_plans,
        "active_subcontracting_orders": active_scos,
        "active_operations": total_operations,
        "active_workstations": total_workstations,
    })


# ---------------------------------------------------------------------------
# ACTIONS dispatch table
# ---------------------------------------------------------------------------

ACTIONS = {
    "add-bom": add_bom,
    "update-bom": update_bom,
    "get-bom": get_bom,
    "list-boms": list_boms,
    "explode-bom": explode_bom,
    "add-operation": add_operation,
    "add-workstation": add_workstation,
    "add-routing": add_routing,
    "add-work-order": add_work_order,
    "get-work-order": get_work_order,
    "list-work-orders": list_work_orders,
    "start-work-order": start_work_order,
    "transfer-materials": transfer_materials,
    "create-job-card": create_job_card,
    "complete-job-card": complete_job_card,
    "complete-work-order": complete_work_order,
    "cancel-work-order": cancel_work_order,
    "create-production-plan": create_production_plan,
    "run-mrp": run_mrp,
    "get-production-plan": get_production_plan,
    "generate-work-orders": generate_work_orders,
    "generate-purchase-requests": generate_purchase_requests,
    "add-subcontracting-order": add_subcontracting_order,
    "status": status_action,
}


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ERPClaw Manufacturing Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Entity IDs
    parser.add_argument("--company-id")
    parser.add_argument("--item-id")
    parser.add_argument("--bom-id")
    parser.add_argument("--work-order-id")
    parser.add_argument("--job-card-id")
    parser.add_argument("--production-plan-id")

    # Master data
    parser.add_argument("--name")
    parser.add_argument("--description")

    # Quantities
    parser.add_argument("--quantity")
    parser.add_argument("--produced-qty")
    parser.add_argument("--for-quantity")
    parser.add_argument("--completed-qty")

    # JSON payloads
    parser.add_argument("--items")       # JSON array
    parser.add_argument("--operations")  # JSON array

    # Operation / workstation / routing references
    parser.add_argument("--routing-id")
    parser.add_argument("--operation-id")
    parser.add_argument("--workstation-id")

    # Workstation fields
    parser.add_argument("--hour-rate")
    parser.add_argument("--time-in-mins")
    parser.add_argument("--actual-time-in-mins")
    parser.add_argument("--workstation-type")
    parser.add_argument("--working-hours-per-day")
    parser.add_argument("--production-capacity")
    parser.add_argument("--holiday-list-id")

    # Dates
    parser.add_argument("--planned-start-date")
    parser.add_argument("--planned-end-date")
    parser.add_argument("--posting-date")

    # Warehouse references
    parser.add_argument("--source-warehouse-id")
    parser.add_argument("--target-warehouse-id")
    parser.add_argument("--wip-warehouse-id")

    # Sales / supplier / subcontracting
    parser.add_argument("--sales-order-id")
    parser.add_argument("--supplier-id")
    parser.add_argument("--service-item-id")
    parser.add_argument("--supplier-warehouse-id")

    # Production planning
    parser.add_argument("--planning-horizon-days")

    # BOM flags
    parser.add_argument("--is-active")
    parser.add_argument("--is-default")
    parser.add_argument("--uom")

    # Filters
    parser.add_argument("--status")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")
    parser.add_argument("--search")

    args, _unknown = parser.parse_known_args()
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
        sys.stderr.write(f"[erpclaw-manufacturing] {e}\n")
        err("An unexpected error occurred")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
