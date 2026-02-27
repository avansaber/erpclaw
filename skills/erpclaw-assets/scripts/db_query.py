#!/usr/bin/env python3
"""ERPClaw Assets Skill -- db_query.py

Fixed asset management, depreciation scheduling, asset movements,
maintenance tracking, and disposal with GL posting.
All 16 actions are routed through this single entry point.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Add shared lib to path
try:
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
    from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH
    from erpclaw_lib.decimal_utils import to_decimal, round_currency
    from erpclaw_lib.naming import get_next_name
    from erpclaw_lib.gl_posting import insert_gl_entries, reverse_gl_entries
    from erpclaw_lib.validation import check_input_lengths
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw-setup first: clawhub install erpclaw-setup", "suggestion": "clawhub install erpclaw-setup"}))
    sys.exit(1)

REQUIRED_TABLES = ["company", "account"]

VALID_DEPRECIATION_METHODS = ("straight_line", "written_down_value", "double_declining")
VALID_ASSET_STATUSES = ("draft", "submitted", "in_use", "scrapped", "sold")
VALID_MOVEMENT_TYPES = ("transfer", "issue", "receipt")
VALID_MAINTENANCE_TYPES = ("preventive", "corrective")
VALID_MAINTENANCE_STATUSES = ("planned", "overdue", "completed")
VALID_DISPOSAL_METHODS = ("sale", "scrap", "write_off")
VALID_SCHEDULE_STATUSES = ("pending", "posted", "skipped")


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


def _validate_company_exists(conn, company_id: str):
    """Validate that a company exists and return the row, or error."""
    company = conn.execute(
        "SELECT id FROM company WHERE id = ?", (company_id,),
    ).fetchone()
    if not company:
        err(f"Company {company_id} not found")
    return company


def _validate_asset_exists(conn, asset_id: str):
    """Validate that an asset exists and return the row, or error."""
    asset = conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        err(f"Asset {asset_id} not found",
             suggestion="Use 'list assets' to see available assets.")
    return asset


def _validate_asset_category_exists(conn, category_id: str):
    """Validate that an asset category exists and return the row, or error."""
    cat = conn.execute(
        "SELECT * FROM asset_category WHERE id = ?", (category_id,),
    ).fetchone()
    if not cat:
        err(f"Asset category {category_id} not found")
    return cat


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD string."""
    return date.today().isoformat()


def _add_months(start_date_str: str, months: int) -> str:
    """Add N months to a date string, returning YYYY-MM-DD."""
    d = date.fromisoformat(start_date_str)
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    # Clamp day to valid range for the target month
    import calendar
    max_day = calendar.monthrange(year, month)[1]
    day = min(d.day, max_day)
    return date(year, month, day).isoformat()


# ---------------------------------------------------------------------------
# 1. add-asset-category
# ---------------------------------------------------------------------------

def add_asset_category(conn, args):
    """Create an asset category.

    Required: --company-id, --name, --depreciation-method, --useful-life-years
    Optional: --asset-account-id, --depreciation-account-id,
              --accumulated-depreciation-account-id
    """
    if not args.company_id:
        err("--company-id is required")
    if not args.name:
        err("--name is required")
    if not args.depreciation_method:
        err("--depreciation-method is required")
    if not args.useful_life_years:
        err("--useful-life-years is required")

    _validate_company_exists(conn, args.company_id)

    method = args.depreciation_method
    if method not in VALID_DEPRECIATION_METHODS:
        err(f"Invalid depreciation method '{method}'. Must be one of: {', '.join(VALID_DEPRECIATION_METHODS)}")

    try:
        useful_life = int(args.useful_life_years)
    except (ValueError, TypeError):
        err("--useful-life-years must be an integer")
    if useful_life <= 0:
        err("--useful-life-years must be greater than 0")

    # Validate account references if provided
    if args.asset_account_id:
        acct = conn.execute("SELECT id FROM account WHERE id = ? OR name = ?", (args.asset_account_id, args.asset_account_id)).fetchone()
        if not acct:
            err(f"Asset account {args.asset_account_id} not found")
        args.asset_account_id = acct["id"]
    if args.depreciation_account_id:
        acct = conn.execute("SELECT id FROM account WHERE id = ? OR name = ?", (args.depreciation_account_id, args.depreciation_account_id)).fetchone()
        if not acct:
            err(f"Depreciation account {args.depreciation_account_id} not found")
        args.depreciation_account_id = acct["id"]
    if args.accumulated_depreciation_account_id:
        acct = conn.execute("SELECT id FROM account WHERE id = ? OR name = ?", (args.accumulated_depreciation_account_id, args.accumulated_depreciation_account_id)).fetchone()
        if not acct:
            err(f"Accumulated depreciation account {args.accumulated_depreciation_account_id} not found")
        args.accumulated_depreciation_account_id = acct["id"]

    # Check for duplicate name in same company
    existing = conn.execute(
        "SELECT id FROM asset_category WHERE name = ? AND company_id = ?",
        (args.name, args.company_id),
    ).fetchone()
    if existing:
        err(f"Asset category '{args.name}' already exists in this company")

    cat_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO asset_category
           (id, name, depreciation_method, useful_life_years,
            asset_account_id, depreciation_account_id,
            accumulated_depreciation_account_id, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (cat_id, args.name, method, useful_life,
         args.asset_account_id, args.depreciation_account_id,
         args.accumulated_depreciation_account_id, args.company_id),
    )

    audit(conn, "erpclaw-assets", "add-asset-category", "asset_category", cat_id,
           new_values={"name": args.name, "depreciation_method": method},
           description=f"Created asset category: {args.name}")

    conn.commit()
    ok({"asset_category_id": cat_id, "name": args.name,
         "message": f"Asset category '{args.name}' created"})


# ---------------------------------------------------------------------------
# 2. list-asset-categories
# ---------------------------------------------------------------------------

def list_asset_categories(conn, args):
    """List all asset categories for a company.

    Required: --company-id
    """
    if not args.company_id:
        err("--company-id is required")

    _validate_company_exists(conn, args.company_id)

    limit = int(args.limit or "20")
    offset = int(args.offset or "0")

    count_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM asset_category WHERE company_id = ?",
        (args.company_id,),
    ).fetchone()
    total = count_row["cnt"]

    rows = conn.execute(
        """SELECT * FROM asset_category WHERE company_id = ?
           ORDER BY name LIMIT ? OFFSET ?""",
        (args.company_id, limit, offset),
    ).fetchall()

    categories = [row_to_dict(r) for r in rows]
    ok({"categories": categories, "total": total, "limit": limit, "offset": offset,
         "has_more": offset + limit < total})


# ---------------------------------------------------------------------------
# 3. add-asset
# ---------------------------------------------------------------------------

def add_asset(conn, args):
    """Create a new asset in draft status.

    Required: --company-id, --name, --asset-category-id, --gross-value
    Optional: --salvage-value, --item-id, --purchase-date, --purchase-invoice-id,
              --depreciation-method, --useful-life-years, --depreciation-start-date,
              --location, --custodian-employee-id, --warranty-expiry-date
    """
    if not args.company_id:
        err("--company-id is required")
    if not args.name:
        err("--name is required")
    if not args.asset_category_id:
        err("--asset-category-id is required")
    if not args.gross_value:
        err("--gross-value is required")

    _validate_company_exists(conn, args.company_id)
    category = _validate_asset_category_exists(conn, args.asset_category_id)
    cat_dict = row_to_dict(category)

    gross_value = to_decimal(args.gross_value)
    if gross_value <= 0:
        err("--gross-value must be greater than 0")

    salvage_value = to_decimal(args.salvage_value or "0")
    if salvage_value < 0:
        err("--salvage-value must be >= 0")
    if salvage_value >= gross_value:
        err("--salvage-value must be less than --gross-value")

    # Depreciation method: override from category if not specified
    dep_method = args.depreciation_method or cat_dict["depreciation_method"]
    if dep_method not in VALID_DEPRECIATION_METHODS:
        err(f"Invalid depreciation method '{dep_method}'. Must be one of: {', '.join(VALID_DEPRECIATION_METHODS)}")

    # Useful life: override from category if not specified
    if args.useful_life_years:
        try:
            useful_life = int(args.useful_life_years)
        except (ValueError, TypeError):
            err("--useful-life-years must be an integer")
        if useful_life <= 0:
            err("--useful-life-years must be greater than 0")
    else:
        useful_life = cat_dict["useful_life_years"]

    # Validate item reference if provided
    if args.item_id:
        item = conn.execute("SELECT id FROM item WHERE id = ?", (args.item_id,)).fetchone()
        if not item:
            err(f"Item {args.item_id} not found")

    # Generate naming series
    naming = get_next_name(conn, "asset", company_id=args.company_id)

    asset_id = str(uuid.uuid4())
    current_book_value = str(round_currency(gross_value))

    conn.execute(
        """INSERT INTO asset
           (id, naming_series, asset_name, asset_category_id, item_id,
            purchase_date, purchase_invoice_id, gross_value, salvage_value,
            depreciation_method, useful_life_years, depreciation_start_date,
            current_book_value, accumulated_depreciation, status,
            location, custodian_employee_id, warranty_expiry_date, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '0', 'draft',
                   ?, ?, ?, ?)""",
        (asset_id, naming, args.name, args.asset_category_id, args.item_id,
         args.purchase_date, args.purchase_invoice_id,
         str(round_currency(gross_value)), str(round_currency(salvage_value)),
         dep_method, useful_life, args.depreciation_start_date,
         current_book_value,
         args.location, args.custodian_employee_id, args.warranty_expiry_date,
         args.company_id),
    )

    audit(conn, "erpclaw-assets", "add-asset", "asset", asset_id,
           new_values={"asset_name": args.name, "naming_series": naming,
                       "gross_value": str(round_currency(gross_value))},
           description=f"Created asset: {naming} - {args.name}")

    conn.commit()
    ok({"asset_id": asset_id, "naming_series": naming, "asset_name": args.name,
         "gross_value": str(round_currency(gross_value)),
         "current_book_value": current_book_value,
         "message": f"Asset '{naming}' created in draft status"})


# ---------------------------------------------------------------------------
# 4. update-asset
# ---------------------------------------------------------------------------

def update_asset(conn, args):
    """Update an asset (only draft or submitted).

    Required: --asset-id
    Optional: --name, --location, --custodian-employee-id,
              --warranty-expiry-date, --status
    """
    if not args.asset_id:
        err("--asset-id is required")

    asset = _validate_asset_exists(conn, args.asset_id)
    asset_dict = row_to_dict(asset)

    if asset_dict["status"] not in ("draft", "submitted"):
        err(f"Cannot update asset in '{asset_dict['status']}' status. "
             f"Only draft or submitted assets can be updated.",
             suggestion="Cancel the document first, then make changes.")

    updates = []
    params = []
    old_values = {}
    new_values = {}

    if args.name is not None:
        old_values["asset_name"] = asset_dict["asset_name"]
        new_values["asset_name"] = args.name
        updates.append("asset_name = ?")
        params.append(args.name)

    if args.location is not None:
        old_values["location"] = asset_dict["location"]
        new_values["location"] = args.location
        updates.append("location = ?")
        params.append(args.location)

    if args.custodian_employee_id is not None:
        old_values["custodian_employee_id"] = asset_dict["custodian_employee_id"]
        new_values["custodian_employee_id"] = args.custodian_employee_id
        updates.append("custodian_employee_id = ?")
        params.append(args.custodian_employee_id)

    if args.warranty_expiry_date is not None:
        old_values["warranty_expiry_date"] = asset_dict["warranty_expiry_date"]
        new_values["warranty_expiry_date"] = args.warranty_expiry_date
        updates.append("warranty_expiry_date = ?")
        params.append(args.warranty_expiry_date)

    if args.status is not None:
        if args.status not in VALID_ASSET_STATUSES:
            err(f"Invalid status '{args.status}'. Must be one of: {', '.join(VALID_ASSET_STATUSES)}")
        old_values["status"] = asset_dict["status"]
        new_values["status"] = args.status
        updates.append("status = ?")
        params.append(args.status)

    if not updates:
        err("No fields to update. Provide at least one of: --name, --location, "
             "--custodian-employee-id, --warranty-expiry-date, --status")

    updates.append("updated_at = datetime('now')")
    params.append(args.asset_id)

    conn.execute(
        f"UPDATE asset SET {', '.join(updates)} WHERE id = ?",
        params,
    )

    audit(conn, "erpclaw-assets", "update-asset", "asset", args.asset_id,
           old_values=old_values, new_values=new_values,
           description=f"Updated asset {asset_dict['naming_series']}")

    conn.commit()
    ok({"asset_id": args.asset_id, "updated_fields": list(new_values.keys()),
         "message": f"Asset {asset_dict['naming_series']} updated"})


# ---------------------------------------------------------------------------
# 5. get-asset
# ---------------------------------------------------------------------------

def get_asset(conn, args):
    """Get an asset with depreciation schedule, movements, maintenance records.

    Required: --asset-id
    """
    if not args.asset_id:
        err("--asset-id is required")

    asset = _validate_asset_exists(conn, args.asset_id)
    asset_dict = row_to_dict(asset)

    # Fetch depreciation schedule
    schedule_rows = conn.execute(
        """SELECT * FROM depreciation_schedule
           WHERE asset_id = ?
           ORDER BY schedule_date""",
        (args.asset_id,),
    ).fetchall()
    asset_dict["depreciation_schedule"] = [row_to_dict(r) for r in schedule_rows]

    # Fetch asset movements
    movement_rows = conn.execute(
        """SELECT * FROM asset_movement
           WHERE asset_id = ?
           ORDER BY movement_date DESC""",
        (args.asset_id,),
    ).fetchall()
    asset_dict["movements"] = [row_to_dict(r) for r in movement_rows]

    # Fetch maintenance records
    maintenance_rows = conn.execute(
        """SELECT * FROM asset_maintenance
           WHERE asset_id = ?
           ORDER BY scheduled_date DESC""",
        (args.asset_id,),
    ).fetchall()
    asset_dict["maintenance"] = [row_to_dict(r) for r in maintenance_rows]

    # Fetch disposal record if any
    disposal_row = conn.execute(
        "SELECT * FROM asset_disposal WHERE asset_id = ?",
        (args.asset_id,),
    ).fetchone()
    asset_dict["disposal"] = row_to_dict(disposal_row) if disposal_row else None

    # Fetch category info
    cat_row = conn.execute(
        "SELECT * FROM asset_category WHERE id = ?",
        (asset_dict["asset_category_id"],),
    ).fetchone()
    asset_dict["category"] = row_to_dict(cat_row) if cat_row else None

    ok({"asset": asset_dict})


# ---------------------------------------------------------------------------
# 6. list-assets
# ---------------------------------------------------------------------------

def list_assets(conn, args):
    """List assets with filters.

    Optional: --company-id, --asset-category-id, --status, --search,
              --limit, --offset
    """
    conditions = []
    params = []

    if args.company_id:
        conditions.append("a.company_id = ?")
        params.append(args.company_id)

    if args.asset_category_id:
        conditions.append("a.asset_category_id = ?")
        params.append(args.asset_category_id)

    if args.status:
        conditions.append("a.status = ?")
        params.append(args.status)

    if args.search:
        conditions.append("a.asset_name LIKE ?")
        params.append(f"%{args.search}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    limit = int(args.limit or "20")
    offset = int(args.offset or "0")

    # Get total count
    count_row = conn.execute(
        f"SELECT COUNT(*) as cnt FROM asset a WHERE {where}", params,
    ).fetchone()
    total = count_row["cnt"]

    # Fetch assets
    rows = conn.execute(
        f"""SELECT a.*, ac.name as category_name
            FROM asset a
            LEFT JOIN asset_category ac ON ac.id = a.asset_category_id
            WHERE {where}
            ORDER BY a.created_at DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    assets = [row_to_dict(r) for r in rows]
    ok({"assets": assets, "total": total, "limit": limit, "offset": offset,
         "has_more": offset + limit < total})


# ---------------------------------------------------------------------------
# 7. generate-depreciation-schedule
# ---------------------------------------------------------------------------

def generate_depreciation_schedule(conn, args):
    """Generate monthly depreciation schedule for an asset.

    Required: --asset-id

    Depreciation methods:
    - straight_line: monthly = (gross - salvage) / (years * 12)
    - written_down_value: annual_rate = 1 - (salvage/gross)^(1/years),
                          monthly = book_value * annual_rate / 12
    - double_declining: annual_rate = 2 / years,
                        monthly = book_value * annual_rate / 12
    """
    if not args.asset_id:
        err("--asset-id is required")

    asset = _validate_asset_exists(conn, args.asset_id)
    asset_dict = row_to_dict(asset)

    dep_method = asset_dict["depreciation_method"]
    if not dep_method:
        err("Asset has no depreciation method set")

    useful_life = asset_dict["useful_life_years"]
    if not useful_life or useful_life <= 0:
        err("Asset has no valid useful life years set")

    start_date = asset_dict["depreciation_start_date"]
    if not start_date:
        err("Asset has no depreciation_start_date set. "
             "Update the asset with --depreciation-start-date first.")

    gross_value = to_decimal(asset_dict["gross_value"])
    salvage_value = to_decimal(asset_dict["salvage_value"])
    depreciable_amount = gross_value - salvage_value

    if depreciable_amount <= 0:
        err("Depreciable amount (gross_value - salvage_value) must be > 0")

    total_months = useful_life * 12

    # Delete existing pending schedule entries (allow regeneration)
    conn.execute(
        "DELETE FROM depreciation_schedule WHERE asset_id = ? AND status = 'pending'",
        (args.asset_id,),
    )

    schedule_entries = []
    accumulated = Decimal("0")
    book_value = gross_value

    if dep_method == "straight_line":
        # Fixed monthly amount
        monthly_amount = round_currency(depreciable_amount / Decimal(str(total_months)))

        for i in range(total_months):
            schedule_date = _add_months(start_date, i)

            # Last month: adjust for rounding to exactly match depreciable amount
            if i == total_months - 1:
                this_amount = depreciable_amount - accumulated
            else:
                this_amount = monthly_amount

            this_amount = round_currency(this_amount)
            accumulated = round_currency(accumulated + this_amount)
            book_value = round_currency(gross_value - accumulated)

            # Don't go below salvage value
            if book_value < salvage_value:
                this_amount = round_currency(this_amount - (salvage_value - book_value))
                accumulated = round_currency(gross_value - salvage_value)
                book_value = salvage_value

            if this_amount <= 0:
                break

            entry_id = str(uuid.uuid4())
            schedule_entries.append({
                "id": entry_id,
                "asset_id": args.asset_id,
                "schedule_date": schedule_date,
                "depreciation_amount": str(this_amount),
                "accumulated_amount": str(accumulated),
                "book_value_after": str(book_value),
                "status": "pending",
                "fiscal_year": schedule_date[:4],
            })

    elif dep_method == "written_down_value":
        # annual_rate = 1 - (salvage / gross) ^ (1 / useful_life)
        if salvage_value <= 0:
            # If salvage is zero, WDV rate is undefined; use straight_line
            # fallback or use a high rate
            err("Written down value method requires salvage_value > 0")

        ratio = salvage_value / gross_value
        # ratio ^ (1/useful_life) using Decimal
        exponent = Decimal("1") / Decimal(str(useful_life))
        # Use float for power calculation, then convert back
        ratio_float = float(ratio)
        exponent_float = float(exponent)
        annual_rate = Decimal(str(1 - ratio_float ** exponent_float))

        for i in range(total_months):
            schedule_date = _add_months(start_date, i)

            # Monthly depreciation = book_value * annual_rate / 12
            this_amount = round_currency(book_value * annual_rate / Decimal("12"))

            # Don't depreciate below salvage value
            if book_value - this_amount < salvage_value:
                this_amount = round_currency(book_value - salvage_value)

            if this_amount <= 0:
                break

            accumulated = round_currency(accumulated + this_amount)
            book_value = round_currency(book_value - this_amount)

            entry_id = str(uuid.uuid4())
            schedule_entries.append({
                "id": entry_id,
                "asset_id": args.asset_id,
                "schedule_date": schedule_date,
                "depreciation_amount": str(this_amount),
                "accumulated_amount": str(accumulated),
                "book_value_after": str(book_value),
                "status": "pending",
                "fiscal_year": schedule_date[:4],
            })

    elif dep_method == "double_declining":
        # annual_rate = 2 / useful_life
        annual_rate = Decimal("2") / Decimal(str(useful_life))

        for i in range(total_months):
            schedule_date = _add_months(start_date, i)

            # Monthly depreciation = book_value * annual_rate / 12
            this_amount = round_currency(book_value * annual_rate / Decimal("12"))

            # Don't depreciate below salvage value
            if book_value - this_amount < salvage_value:
                this_amount = round_currency(book_value - salvage_value)

            if this_amount <= 0:
                break

            accumulated = round_currency(accumulated + this_amount)
            book_value = round_currency(book_value - this_amount)

            entry_id = str(uuid.uuid4())
            schedule_entries.append({
                "id": entry_id,
                "asset_id": args.asset_id,
                "schedule_date": schedule_date,
                "depreciation_amount": str(this_amount),
                "accumulated_amount": str(accumulated),
                "book_value_after": str(book_value),
                "status": "pending",
                "fiscal_year": schedule_date[:4],
            })

    else:
        err(f"Unsupported depreciation method: {dep_method}")

    # Insert schedule entries
    for entry in schedule_entries:
        conn.execute(
            """INSERT INTO depreciation_schedule
               (id, asset_id, schedule_date, depreciation_amount,
                accumulated_amount, book_value_after, status, fiscal_year)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry["id"], entry["asset_id"], entry["schedule_date"],
             entry["depreciation_amount"], entry["accumulated_amount"],
             entry["book_value_after"], entry["status"], entry["fiscal_year"]),
        )

    audit(conn, "erpclaw-assets", "generate-depreciation-schedule", "asset", args.asset_id,
           new_values={"entries_count": len(schedule_entries),
                       "method": dep_method,
                       "total_months": total_months},
           description=f"Generated {len(schedule_entries)} depreciation schedule entries")

    conn.commit()
    ok({"asset_id": args.asset_id,
         "entries_generated": len(schedule_entries),
         "depreciation_method": dep_method,
         "total_depreciable_amount": str(depreciable_amount),
         "schedule": schedule_entries,
         "message": f"Generated {len(schedule_entries)} depreciation schedule entries"})


# ---------------------------------------------------------------------------
# 8. post-depreciation
# ---------------------------------------------------------------------------

def post_depreciation(conn, args):
    """Post a single depreciation entry with GL entries.

    Required: --depreciation-schedule-id (or --asset-id + --posting-date)
    Optional: --cost-center-id

    GL entries:
    - DR Depreciation Expense account (from category)
    - CR Accumulated Depreciation account (from category)
    """
    schedule_entry = None

    if args.depreciation_schedule_id:
        schedule_entry = conn.execute(
            "SELECT * FROM depreciation_schedule WHERE id = ?",
            (args.depreciation_schedule_id,),
        ).fetchone()
        if not schedule_entry:
            err(f"Depreciation schedule entry {args.depreciation_schedule_id} not found")
    elif args.asset_id and args.posting_date:
        # Find the schedule entry for this asset on or before posting_date
        schedule_entry = conn.execute(
            """SELECT * FROM depreciation_schedule
               WHERE asset_id = ? AND schedule_date <= ? AND status = 'pending'
               ORDER BY schedule_date ASC LIMIT 1""",
            (args.asset_id, args.posting_date),
        ).fetchone()
        if not schedule_entry:
            err(f"No pending depreciation schedule entry found for asset {args.asset_id} "
                 f"on or before {args.posting_date}")
    else:
        err("Provide --depreciation-schedule-id, or --asset-id + --posting-date")

    sched_dict = row_to_dict(schedule_entry)

    if sched_dict["status"] != "pending":
        err(f"Schedule entry is already '{sched_dict['status']}', not 'pending'")

    # Fetch asset
    asset = _validate_asset_exists(conn, sched_dict["asset_id"])
    asset_dict = row_to_dict(asset)

    if asset_dict["status"] not in ("submitted", "in_use"):
        err(f"Asset status is '{asset_dict['status']}'. "
             f"Only submitted or in_use assets can have depreciation posted.")

    # Fetch category for accounts
    category = _validate_asset_category_exists(conn, asset_dict["asset_category_id"])
    cat_dict = row_to_dict(category)

    dep_account_id = cat_dict.get("depreciation_account_id")
    accum_dep_account_id = cat_dict.get("accumulated_depreciation_account_id")

    if not dep_account_id:
        err("Asset category has no depreciation_account_id set")
    if not accum_dep_account_id:
        err("Asset category has no accumulated_depreciation_account_id set")

    posting_date = args.posting_date or sched_dict["schedule_date"]
    dep_amount = sched_dict["depreciation_amount"]

    # Fiscal year
    fiscal_year = _get_fiscal_year(conn, posting_date)

    # Cost center
    cost_center_id = args.cost_center_id or _get_cost_center(conn, asset_dict["company_id"])

    # Prepare GL entries
    voucher_id = sched_dict["id"]
    gl_entries = [
        {
            "account_id": dep_account_id,
            "debit": dep_amount,
            "credit": "0",
            "cost_center_id": cost_center_id,
            "fiscal_year": fiscal_year,
        },
        {
            "account_id": accum_dep_account_id,
            "debit": "0",
            "credit": dep_amount,
            "cost_center_id": cost_center_id,
            "fiscal_year": fiscal_year,
        },
    ]

    try:
        gl_ids = insert_gl_entries(
            conn, gl_entries,
            voucher_type="depreciation_entry",
            voucher_id=voucher_id,
            posting_date=posting_date,
            company_id=asset_dict["company_id"],
            remarks=f"Depreciation for {asset_dict['naming_series']} on {posting_date}",
        )
    except (ValueError, NotImplementedError) as e:
        sys.stderr.write(f"[erpclaw-assets] {e}\n")
        err(f"GL posting failed: {e}")

    # Update depreciation_schedule entry
    conn.execute(
        """UPDATE depreciation_schedule
           SET status = 'posted', journal_entry_id = ?
           WHERE id = ?""",
        (voucher_id, sched_dict["id"]),
    )

    # Update asset: current_book_value and accumulated_depreciation
    new_accum = round_currency(
        to_decimal(asset_dict["accumulated_depreciation"]) + to_decimal(dep_amount)
    )
    new_book_value = round_currency(
        to_decimal(asset_dict["gross_value"]) - new_accum
    )

    conn.execute(
        """UPDATE asset
           SET current_book_value = ?,
               accumulated_depreciation = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (str(new_book_value), str(new_accum), sched_dict["asset_id"]),
    )

    audit(conn, "erpclaw-assets", "post-depreciation", "asset", sched_dict["asset_id"],
           old_values={"current_book_value": asset_dict["current_book_value"],
                       "accumulated_depreciation": asset_dict["accumulated_depreciation"]},
           new_values={"current_book_value": str(new_book_value),
                       "accumulated_depreciation": str(new_accum)},
           description=f"Posted depreciation of {dep_amount} for {asset_dict['naming_series']}")

    conn.commit()
    ok({"asset_id": sched_dict["asset_id"],
         "schedule_id": sched_dict["id"],
         "depreciation_amount": dep_amount,
         "new_book_value": str(new_book_value),
         "new_accumulated_depreciation": str(new_accum),
         "gl_entry_ids": gl_ids,
         "message": f"Depreciation of {dep_amount} posted for {asset_dict['naming_series']}"})


# ---------------------------------------------------------------------------
# 9. run-depreciation
# ---------------------------------------------------------------------------

def run_depreciation(conn, args):
    """Batch depreciation posting for all pending entries up to a date.

    Required: --company-id, --posting-date
    Optional: --cost-center-id

    Finds all pending depreciation_schedule entries with schedule_date <= posting_date
    for assets in this company and posts each one.
    """
    if not args.company_id:
        err("--company-id is required")
    if not args.posting_date:
        err("--posting-date is required")

    _validate_company_exists(conn, args.company_id)

    # Find all pending entries for assets in this company
    pending_entries = conn.execute(
        """SELECT ds.*, a.company_id, a.asset_category_id, a.naming_series,
                  a.gross_value, a.accumulated_depreciation, a.current_book_value,
                  a.status as asset_status
           FROM depreciation_schedule ds
           JOIN asset a ON a.id = ds.asset_id
           WHERE a.company_id = ?
             AND ds.schedule_date <= ?
             AND ds.status = 'pending'
             AND a.status IN ('submitted', 'in_use')
           ORDER BY ds.schedule_date ASC""",
        (args.company_id, args.posting_date),
    ).fetchall()

    if not pending_entries:
        ok({"entries_posted": 0, "message": "No pending depreciation entries found"})

    cost_center_id = args.cost_center_id or _get_cost_center(conn, args.company_id)
    fiscal_year = _get_fiscal_year(conn, args.posting_date)

    posted_count = 0
    errors = []
    posted_details = []

    for entry_row in pending_entries:
        entry = row_to_dict(entry_row)

        # Fetch category for accounts
        cat = conn.execute(
            "SELECT * FROM asset_category WHERE id = ?",
            (entry["asset_category_id"],),
        ).fetchone()
        if not cat:
            errors.append(f"Category not found for asset {entry['asset_id']}")
            continue
        cat_dict = row_to_dict(cat)

        dep_account_id = cat_dict.get("depreciation_account_id")
        accum_dep_account_id = cat_dict.get("accumulated_depreciation_account_id")

        if not dep_account_id or not accum_dep_account_id:
            errors.append(
                f"Missing depreciation accounts for category {cat_dict['name']} "
                f"(asset {entry['naming_series']})"
            )
            continue

        dep_amount = entry["depreciation_amount"]
        voucher_id = entry["id"]

        gl_entries = [
            {
                "account_id": dep_account_id,
                "debit": dep_amount,
                "credit": "0",
                "cost_center_id": cost_center_id,
                "fiscal_year": fiscal_year,
            },
            {
                "account_id": accum_dep_account_id,
                "debit": "0",
                "credit": dep_amount,
                "cost_center_id": cost_center_id,
                "fiscal_year": fiscal_year,
            },
        ]

        try:
            gl_ids = insert_gl_entries(
                conn, gl_entries,
                voucher_type="depreciation_entry",
                voucher_id=voucher_id,
                posting_date=args.posting_date,
                company_id=args.company_id,
                remarks=f"Batch depreciation for {entry['naming_series']}",
            )
        except (ValueError, NotImplementedError) as e:
            sys.stderr.write(f"[erpclaw-assets] GL posting for {entry['naming_series']}: {e}\n")
            errors.append(f"GL posting failed for {entry['naming_series']}")
            continue

        # Update schedule entry
        conn.execute(
            """UPDATE depreciation_schedule
               SET status = 'posted', journal_entry_id = ?
               WHERE id = ?""",
            (voucher_id, entry["id"]),
        )

        # Re-read asset for current values (may have been updated by previous entry in batch)
        current_asset = conn.execute(
            "SELECT accumulated_depreciation, gross_value FROM asset WHERE id = ?",
            (entry["asset_id"],),
        ).fetchone()
        current_asset_dict = row_to_dict(current_asset)

        new_accum = round_currency(
            to_decimal(current_asset_dict["accumulated_depreciation"]) + to_decimal(dep_amount)
        )
        new_book_value = round_currency(
            to_decimal(current_asset_dict["gross_value"]) - new_accum
        )

        conn.execute(
            """UPDATE asset
               SET current_book_value = ?,
                   accumulated_depreciation = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (str(new_book_value), str(new_accum), entry["asset_id"]),
        )

        posted_count += 1
        posted_details.append({
            "asset_id": entry["asset_id"],
            "naming_series": entry["naming_series"],
            "schedule_id": entry["id"],
            "amount": dep_amount,
            "new_book_value": str(new_book_value),
        })

    audit(conn, "erpclaw-assets", "run-depreciation", "asset", args.company_id,
           new_values={"posted_count": posted_count, "posting_date": args.posting_date},
           description=f"Batch depreciation: {posted_count} entries posted")

    conn.commit()

    result = {
        "entries_posted": posted_count,
        "posting_date": args.posting_date,
        "details": posted_details,
        "message": f"Posted {posted_count} depreciation entries",
    }
    if errors:
        result["errors"] = errors

    ok(result)


# ---------------------------------------------------------------------------
# 10. record-asset-movement
# ---------------------------------------------------------------------------

def record_asset_movement(conn, args):
    """Record an asset movement (transfer, issue, receipt).

    Required: --asset-id, --movement-type, --movement-date
    Optional: --from-location, --to-location, --from-employee-id,
              --to-employee-id, --reason
    """
    if not args.asset_id:
        err("--asset-id is required")
    if not args.movement_type:
        err("--movement-type is required")
    if not args.movement_date:
        err("--movement-date is required")

    asset = _validate_asset_exists(conn, args.asset_id)
    asset_dict = row_to_dict(asset)

    movement_type = args.movement_type
    if movement_type not in VALID_MOVEMENT_TYPES:
        err(f"Invalid movement type '{movement_type}'. "
             f"Must be one of: {', '.join(VALID_MOVEMENT_TYPES)}")

    # Validate movement makes sense
    if movement_type == "transfer":
        if not args.to_location and not args.to_employee_id:
            err("Transfer requires --to-location or --to-employee-id")

    movement_id = str(uuid.uuid4())

    conn.execute(
        """INSERT INTO asset_movement
           (id, asset_id, movement_type, from_location, to_location,
            from_employee_id, to_employee_id, movement_date, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (movement_id, args.asset_id, movement_type,
         args.from_location or asset_dict.get("location"),
         args.to_location,
         args.from_employee_id or asset_dict.get("custodian_employee_id"),
         args.to_employee_id,
         args.movement_date, args.reason),
    )

    # Update asset location and custodian based on movement
    update_fields = []
    update_params = []
    old_values = {}
    new_values = {}

    if args.to_location is not None:
        old_values["location"] = asset_dict.get("location")
        new_values["location"] = args.to_location
        update_fields.append("location = ?")
        update_params.append(args.to_location)

    if args.to_employee_id is not None:
        old_values["custodian_employee_id"] = asset_dict.get("custodian_employee_id")
        new_values["custodian_employee_id"] = args.to_employee_id
        update_fields.append("custodian_employee_id = ?")
        update_params.append(args.to_employee_id)

    if update_fields:
        update_fields.append("updated_at = datetime('now')")
        update_params.append(args.asset_id)
        conn.execute(
            f"UPDATE asset SET {', '.join(update_fields)} WHERE id = ?",
            update_params,
        )

    audit(conn, "erpclaw-assets", "record-asset-movement", "asset_movement", movement_id,
           old_values=old_values, new_values=new_values,
           description=f"Asset movement ({movement_type}) for {asset_dict['naming_series']}")

    conn.commit()
    ok({"movement_id": movement_id, "asset_id": args.asset_id,
         "movement_type": movement_type,
         "message": f"Asset movement recorded for {asset_dict['naming_series']}"})


# ---------------------------------------------------------------------------
# 11. schedule-maintenance
# ---------------------------------------------------------------------------

def schedule_maintenance(conn, args):
    """Schedule a maintenance task for an asset.

    Required: --asset-id, --maintenance-type, --scheduled-date
    Optional: --description, --next-due-date
    """
    if not args.asset_id:
        err("--asset-id is required")
    if not args.maintenance_type:
        err("--maintenance-type is required")
    if not args.scheduled_date:
        err("--scheduled-date is required")

    asset = _validate_asset_exists(conn, args.asset_id)
    asset_dict = row_to_dict(asset)

    maint_type = args.maintenance_type
    if maint_type not in VALID_MAINTENANCE_TYPES:
        err(f"Invalid maintenance type '{maint_type}'. "
             f"Must be one of: {', '.join(VALID_MAINTENANCE_TYPES)}")

    maint_id = str(uuid.uuid4())

    conn.execute(
        """INSERT INTO asset_maintenance
           (id, asset_id, maintenance_type, scheduled_date, description,
            next_due_date, status)
           VALUES (?, ?, ?, ?, ?, ?, 'planned')""",
        (maint_id, args.asset_id, maint_type, args.scheduled_date,
         args.description, args.next_due_date),
    )

    audit(conn, "erpclaw-assets", "schedule-maintenance", "asset_maintenance", maint_id,
           new_values={"maintenance_type": maint_type,
                       "scheduled_date": args.scheduled_date},
           description=f"Scheduled {maint_type} maintenance for {asset_dict['naming_series']}")

    conn.commit()
    ok({"maintenance_id": maint_id, "asset_id": args.asset_id,
         "maintenance_type": maint_type,
         "scheduled_date": args.scheduled_date,
         "message": f"Maintenance scheduled for {asset_dict['naming_series']}"})


# ---------------------------------------------------------------------------
# 12. complete-maintenance
# ---------------------------------------------------------------------------

def complete_maintenance(conn, args):
    """Mark a maintenance task as completed.

    Required: --maintenance-id
    Optional: --actual-date (defaults to today), --cost, --performed-by, --description
    """
    if not args.maintenance_id:
        err("--maintenance-id is required")

    maint = conn.execute(
        "SELECT * FROM asset_maintenance WHERE id = ?",
        (args.maintenance_id,),
    ).fetchone()
    if not maint:
        err(f"Maintenance record {args.maintenance_id} not found")

    maint_dict = row_to_dict(maint)

    if maint_dict["status"] == "completed":
        err("Maintenance is already completed")

    actual_date = args.actual_date or _today_str()
    cost = args.cost or maint_dict.get("cost", "0")
    performed_by = args.performed_by
    description = args.description or maint_dict.get("description")

    old_values = {"status": maint_dict["status"]}

    conn.execute(
        """UPDATE asset_maintenance
           SET status = 'completed',
               actual_date = ?,
               cost = ?,
               performed_by = ?,
               description = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (actual_date, cost, performed_by, description, args.maintenance_id),
    )

    audit(conn, "erpclaw-assets", "complete-maintenance", "asset_maintenance", args.maintenance_id,
           old_values=old_values,
           new_values={"status": "completed", "actual_date": actual_date, "cost": cost},
           description=f"Completed maintenance {args.maintenance_id}")

    conn.commit()
    ok({"maintenance_id": args.maintenance_id,
         "actual_date": actual_date,
         "cost": cost,
         "message": "Maintenance completed"})


# ---------------------------------------------------------------------------
# 13. dispose-asset
# ---------------------------------------------------------------------------

def dispose_asset(conn, args):
    """Dispose of an asset (sale, scrap, or write_off).

    Required: --asset-id, --disposal-date, --disposal-method
    Optional: --sale-amount, --buyer-details, --cost-center-id

    GL entries for disposal:
    - DR Accumulated Depreciation (full accumulated amount)
    - DR Asset Disposal/Loss (if loss) OR CR Gain on Disposal (if gain)
    - CR Asset Account (gross_value)

    For scrap/write_off: sale_amount = 0, loss = current_book_value
    """
    if not args.asset_id:
        err("--asset-id is required")
    if not args.disposal_date:
        err("--disposal-date is required")
    if not args.disposal_method:
        err("--disposal-method is required")

    asset = _validate_asset_exists(conn, args.asset_id)
    asset_dict = row_to_dict(asset)

    if asset_dict["status"] in ("scrapped", "sold"):
        err(f"Asset is already '{asset_dict['status']}'. Cannot dispose again.")

    if asset_dict["status"] == "draft":
        err("Cannot dispose a draft asset. Submit the asset first.")

    disposal_method = args.disposal_method
    if disposal_method not in VALID_DISPOSAL_METHODS:
        err(f"Invalid disposal method '{disposal_method}'. "
             f"Must be one of: {', '.join(VALID_DISPOSAL_METHODS)}")

    # Fetch category for accounts
    category = _validate_asset_category_exists(conn, asset_dict["asset_category_id"])
    cat_dict = row_to_dict(category)

    asset_account_id = cat_dict.get("asset_account_id")
    accum_dep_account_id = cat_dict.get("accumulated_depreciation_account_id")
    dep_account_id = cat_dict.get("depreciation_account_id")

    if not asset_account_id:
        err("Asset category has no asset_account_id set")
    if not accum_dep_account_id:
        err("Asset category has no accumulated_depreciation_account_id set")
    if not dep_account_id:
        err("Asset category has no depreciation_account_id set (needed for gain/loss)")

    gross_value = to_decimal(asset_dict["gross_value"])
    current_book_value = to_decimal(asset_dict["current_book_value"])
    accumulated_depreciation = to_decimal(asset_dict["accumulated_depreciation"])

    # Determine sale amount
    if disposal_method == "sale":
        sale_amount = to_decimal(args.sale_amount or "0")
    else:
        # Scrap / write_off: no sale proceeds
        sale_amount = Decimal("0")

    # Calculate gain or loss
    # gain_or_loss = sale_amount - current_book_value
    # Positive = gain, Negative = loss
    gain_or_loss = round_currency(sale_amount - current_book_value)

    # Create disposal record
    disposal_id = str(uuid.uuid4())

    conn.execute(
        """INSERT INTO asset_disposal
           (id, asset_id, disposal_date, disposal_method, sale_amount,
            book_value_at_disposal, gain_or_loss, buyer_details)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (disposal_id, args.asset_id, args.disposal_date, disposal_method,
         str(round_currency(sale_amount)),
         str(round_currency(current_book_value)),
         str(gain_or_loss),
         args.buyer_details),
    )

    # Post GL entries for disposal
    #
    # GL layout (double-entry balanced):
    #
    # Sale at gain:
    #   DR Accumulated Depreciation   (accumulated_depreciation)
    #   DR Cash/Receivable            (sale_amount)
    #   CR Fixed Asset Account        (gross_value)
    #   CR Gain on Disposal           (gain_or_loss)
    #
    # Sale at loss:
    #   DR Accumulated Depreciation   (accumulated_depreciation)
    #   DR Cash/Receivable            (sale_amount)
    #   DR Loss on Disposal           (abs(gain_or_loss))
    #   CR Fixed Asset Account        (gross_value)
    #
    # Scrap (sale=0, loss = book_value):
    #   DR Accumulated Depreciation   (accumulated_depreciation)
    #   DR Loss on Disposal           (book_value)
    #   CR Fixed Asset Account        (gross_value)
    #
    cost_center_id = args.cost_center_id or _get_cost_center(conn, asset_dict["company_id"])
    fiscal_year = _get_fiscal_year(conn, args.disposal_date)

    gl_entries = []

    # DR Accumulated Depreciation
    if accumulated_depreciation > 0:
        gl_entries.append({
            "account_id": accum_dep_account_id,
            "debit": str(round_currency(accumulated_depreciation)),
            "credit": "0",
            "cost_center_id": cost_center_id,
            "fiscal_year": fiscal_year,
        })

    # CR Fixed Asset Account (at gross value)
    gl_entries.append({
        "account_id": asset_account_id,
        "debit": "0",
        "credit": str(round_currency(gross_value)),
        "cost_center_id": cost_center_id,
        "fiscal_year": fiscal_year,
    })

    if gain_or_loss < Decimal("0"):
        # Loss on disposal
        # DR Loss account for abs(gain_or_loss)
        gl_entries.append({
            "account_id": dep_account_id,
            "debit": str(round_currency(abs(gain_or_loss))),
            "credit": "0",
            "cost_center_id": cost_center_id,
            "fiscal_year": fiscal_year,
        })
        # If there's a sale amount, DR the sale proceeds too
        if sale_amount > 0:
            gl_entries.append({
                "account_id": dep_account_id,
                "debit": str(round_currency(sale_amount)),
                "credit": "0",
                "cost_center_id": cost_center_id,
                "fiscal_year": fiscal_year,
            })
    elif gain_or_loss > Decimal("0"):
        # Gain on disposal: sale > book_value
        # DR Sale proceeds
        gl_entries.append({
            "account_id": dep_account_id,
            "debit": str(round_currency(sale_amount)),
            "credit": "0",
            "cost_center_id": cost_center_id,
            "fiscal_year": fiscal_year,
        })
        # CR Gain
        gl_entries.append({
            "account_id": dep_account_id,
            "debit": "0",
            "credit": str(round_currency(gain_or_loss)),
            "cost_center_id": cost_center_id,
            "fiscal_year": fiscal_year,
        })
    else:
        # No gain/loss: sale_amount == book_value
        if sale_amount > 0:
            gl_entries.append({
                "account_id": dep_account_id,
                "debit": str(round_currency(sale_amount)),
                "credit": "0",
                "cost_center_id": cost_center_id,
                "fiscal_year": fiscal_year,
            })

    try:
        gl_ids = insert_gl_entries(
            conn, gl_entries,
            voucher_type="asset_disposal",
            voucher_id=disposal_id,
            posting_date=args.disposal_date,
            company_id=asset_dict["company_id"],
            remarks=f"Disposal ({disposal_method}) of {asset_dict['naming_series']}",
        )
    except (ValueError, NotImplementedError) as e:
        sys.stderr.write(f"[erpclaw-assets] {e}\n")
        err(f"GL posting failed: {e}")

    # Update disposal record with journal reference
    conn.execute(
        "UPDATE asset_disposal SET journal_entry_id = ? WHERE id = ?",
        (disposal_id, disposal_id),
    )

    # Update asset status
    new_status = "sold" if disposal_method == "sale" else "scrapped"
    conn.execute(
        """UPDATE asset
           SET status = ?,
               current_book_value = '0',
               updated_at = datetime('now')
           WHERE id = ?""",
        (new_status, args.asset_id),
    )

    audit(conn, "erpclaw-assets", "dispose-asset", "asset", args.asset_id,
           old_values={"status": asset_dict["status"],
                       "current_book_value": asset_dict["current_book_value"]},
           new_values={"status": new_status, "disposal_method": disposal_method,
                       "sale_amount": str(round_currency(sale_amount)),
                       "gain_or_loss": str(gain_or_loss)},
           description=f"Disposed asset {asset_dict['naming_series']} via {disposal_method}")

    conn.commit()
    ok({"disposal_id": disposal_id,
         "asset_id": args.asset_id,
         "disposal_method": disposal_method,
         "sale_amount": str(round_currency(sale_amount)),
         "book_value_at_disposal": str(round_currency(current_book_value)),
         "gain_or_loss": str(gain_or_loss),
         "new_status": new_status,
         "gl_entry_ids": gl_ids,
         "message": f"Asset {asset_dict['naming_series']} disposed via {disposal_method}"})


# ---------------------------------------------------------------------------
# 14. asset-register-report
# ---------------------------------------------------------------------------

def asset_register_report(conn, args):
    """Generate an asset register report.

    Required: --company-id
    Optional: --as-of-date (defaults to today)

    Returns all assets with gross_value, accumulated_depreciation, current_book_value.
    """
    if not args.company_id:
        err("--company-id is required")

    _validate_company_exists(conn, args.company_id)
    as_of_date = args.as_of_date or _today_str()

    # Fetch all assets for the company
    assets = conn.execute(
        """SELECT a.id, a.naming_series, a.asset_name, a.asset_category_id,
                  a.purchase_date, a.gross_value, a.salvage_value,
                  a.depreciation_method, a.useful_life_years,
                  a.status, a.location, a.custodian_employee_id,
                  ac.name as category_name
           FROM asset a
           LEFT JOIN asset_category ac ON ac.id = a.asset_category_id
           WHERE a.company_id = ?
           ORDER BY ac.name, a.naming_series""",
        (args.company_id,),
    ).fetchall()

    register = []
    total_gross = Decimal("0")
    total_accum_dep = Decimal("0")
    total_book_value = Decimal("0")

    for asset_row in assets:
        asset = row_to_dict(asset_row)
        asset_id = asset["id"]
        gross = to_decimal(asset["gross_value"])

        # Calculate accumulated depreciation as of the date using Decimal
        posted_dep_entries = conn.execute(
            """SELECT depreciation_amount
               FROM depreciation_schedule
               WHERE asset_id = ? AND status = 'posted' AND schedule_date <= ?""",
            (asset_id, as_of_date),
        ).fetchall()

        accum_dep = Decimal("0")
        for dep_row in posted_dep_entries:
            accum_dep = round_currency(accum_dep + to_decimal(dep_row["depreciation_amount"]))

        book_value = round_currency(gross - accum_dep)

        total_gross = round_currency(total_gross + gross)
        total_accum_dep = round_currency(total_accum_dep + accum_dep)
        total_book_value = round_currency(total_book_value + book_value)

        register.append({
            "asset_id": asset_id,
            "naming_series": asset["naming_series"],
            "asset_name": asset["asset_name"],
            "category_name": asset["category_name"],
            "purchase_date": asset["purchase_date"],
            "gross_value": str(gross),
            "accumulated_depreciation": str(accum_dep),
            "current_book_value": str(book_value),
            "status": asset["status"],
            "location": asset["location"],
        })

    ok({
        "report": "Asset Register",
        "company_id": args.company_id,
        "as_of_date": as_of_date,
        "assets": register,
        "summary": {
            "total_assets": len(register),
            "total_gross_value": str(total_gross),
            "total_accumulated_depreciation": str(total_accum_dep),
            "total_book_value": str(total_book_value),
        },
    })


# ---------------------------------------------------------------------------
# 15. depreciation-summary
# ---------------------------------------------------------------------------

def depreciation_summary(conn, args):
    """Depreciation summary report grouped by asset category.

    Required: --company-id
    Optional: --from-date, --to-date
    """
    if not args.company_id:
        err("--company-id is required")

    _validate_company_exists(conn, args.company_id)

    conditions = ["a.company_id = ?", "ds.status = 'posted'"]
    params = [args.company_id]

    if args.from_date:
        conditions.append("ds.schedule_date >= ?")
        params.append(args.from_date)

    if args.to_date:
        conditions.append("ds.schedule_date <= ?")
        params.append(args.to_date)

    where = " AND ".join(conditions)

    # Fetch posted depreciation entries grouped by category
    rows = conn.execute(
        f"""SELECT ac.id as category_id, ac.name as category_name,
                   a.id as asset_id, a.naming_series, a.asset_name,
                   ds.depreciation_amount, ds.schedule_date
            FROM depreciation_schedule ds
            JOIN asset a ON a.id = ds.asset_id
            JOIN asset_category ac ON ac.id = a.asset_category_id
            WHERE {where}
            ORDER BY ac.name, a.naming_series, ds.schedule_date""",
        params,
    ).fetchall()

    # Group by category
    categories = {}
    grand_total = Decimal("0")

    for row in rows:
        r = row_to_dict(row)
        cat_id = r["category_id"]
        cat_name = r["category_name"]
        dep_amount = to_decimal(r["depreciation_amount"])

        if cat_id not in categories:
            categories[cat_id] = {
                "category_id": cat_id,
                "category_name": cat_name,
                "total_depreciation": Decimal("0"),
                "assets": {},
            }

        categories[cat_id]["total_depreciation"] = round_currency(
            categories[cat_id]["total_depreciation"] + dep_amount
        )

        asset_id = r["asset_id"]
        if asset_id not in categories[cat_id]["assets"]:
            categories[cat_id]["assets"][asset_id] = {
                "asset_id": asset_id,
                "naming_series": r["naming_series"],
                "asset_name": r["asset_name"],
                "total_depreciation": Decimal("0"),
                "entries_count": 0,
            }

        categories[cat_id]["assets"][asset_id]["total_depreciation"] = round_currency(
            categories[cat_id]["assets"][asset_id]["total_depreciation"] + dep_amount
        )
        categories[cat_id]["assets"][asset_id]["entries_count"] += 1
        grand_total = round_currency(grand_total + dep_amount)

    # Convert to serializable format
    summary = []
    for cat_id, cat_data in categories.items():
        assets_list = []
        for asset_data in cat_data["assets"].values():
            assets_list.append({
                "asset_id": asset_data["asset_id"],
                "naming_series": asset_data["naming_series"],
                "asset_name": asset_data["asset_name"],
                "total_depreciation": str(asset_data["total_depreciation"]),
                "entries_count": asset_data["entries_count"],
            })
        summary.append({
            "category_id": cat_data["category_id"],
            "category_name": cat_data["category_name"],
            "total_depreciation": str(cat_data["total_depreciation"]),
            "assets": assets_list,
        })

    ok({
        "report": "Depreciation Summary",
        "company_id": args.company_id,
        "from_date": args.from_date,
        "to_date": args.to_date,
        "categories": summary,
        "grand_total_depreciation": str(grand_total),
    })


# ---------------------------------------------------------------------------
# 16. status
# ---------------------------------------------------------------------------

def status_action(conn, args):
    """Dashboard: total assets by status, total book value, pending depreciation,
    upcoming maintenance.

    Required: --company-id
    """
    company_id = args.company_id
    if not company_id:
        row = conn.execute("SELECT id FROM company LIMIT 1").fetchone()
        if not row:
            err("No company found. Create one with erpclaw-setup first.",
                 suggestion="Run 'tutorial' to create a demo company, or 'setup company' to create your own.")
        company_id = row["id"]

    _validate_company_exists(conn, company_id)

    # Assets by status
    status_rows = conn.execute(
        """SELECT status, COUNT(*) as count
           FROM asset WHERE company_id = ?
           GROUP BY status""",
        (company_id,),
    ).fetchall()
    assets_by_status = {r["status"]: r["count"] for r in status_rows}

    # Total book value
    bv_row = conn.execute(
        """SELECT COALESCE(COUNT(*), 0) as total_assets
           FROM asset WHERE company_id = ?""",
        (company_id,),
    ).fetchone()

    # Calculate total book value using Decimal
    asset_bv_rows = conn.execute(
        "SELECT current_book_value FROM asset WHERE company_id = ?",
        (company_id,),
    ).fetchall()
    total_book_value = Decimal("0")
    for r in asset_bv_rows:
        total_book_value = round_currency(total_book_value + to_decimal(r["current_book_value"]))

    # Total gross value
    asset_gv_rows = conn.execute(
        "SELECT gross_value FROM asset WHERE company_id = ?",
        (company_id,),
    ).fetchall()
    total_gross_value = Decimal("0")
    for r in asset_gv_rows:
        total_gross_value = round_currency(total_gross_value + to_decimal(r["gross_value"]))

    # Pending depreciation entries
    pending_dep = conn.execute(
        """SELECT COUNT(*) as count
           FROM depreciation_schedule ds
           JOIN asset a ON a.id = ds.asset_id
           WHERE a.company_id = ? AND ds.status = 'pending'""",
        (company_id,),
    ).fetchone()

    # Overdue depreciation (pending entries with schedule_date < today)
    today = _today_str()
    overdue_dep = conn.execute(
        """SELECT COUNT(*) as count
           FROM depreciation_schedule ds
           JOIN asset a ON a.id = ds.asset_id
           WHERE a.company_id = ? AND ds.status = 'pending' AND ds.schedule_date < ?""",
        (company_id, today),
    ).fetchone()

    # Upcoming maintenance (next 30 days)
    thirty_days = (date.today() + timedelta(days=30)).isoformat()
    upcoming_maint = conn.execute(
        """SELECT am.*, a.naming_series, a.asset_name
           FROM asset_maintenance am
           JOIN asset a ON a.id = am.asset_id
           WHERE a.company_id = ?
             AND am.status IN ('planned', 'overdue')
             AND am.scheduled_date <= ?
           ORDER BY am.scheduled_date ASC
           LIMIT 10""",
        (company_id, thirty_days),
    ).fetchall()
    upcoming_maint_list = [row_to_dict(r) for r in upcoming_maint]

    # Overdue maintenance
    overdue_maint = conn.execute(
        """SELECT COUNT(*) as count
           FROM asset_maintenance am
           JOIN asset a ON a.id = am.asset_id
           WHERE a.company_id = ?
             AND am.status IN ('planned', 'overdue')
             AND am.scheduled_date < ?""",
        (company_id, today),
    ).fetchone()

    ok({
        "dashboard": "Asset Management Status",
        "company_id": company_id,
        "assets_by_status": assets_by_status,
        "total_assets": bv_row["total_assets"],
        "total_gross_value": str(total_gross_value),
        "total_book_value": str(total_book_value),
        "total_accumulated_depreciation": str(
            round_currency(total_gross_value - total_book_value)
        ),
        "pending_depreciation_entries": pending_dep["count"],
        "overdue_depreciation_entries": overdue_dep["count"],
        "upcoming_maintenance": upcoming_maint_list,
        "overdue_maintenance_count": overdue_maint["count"],
    })


# ---------------------------------------------------------------------------
# ACTIONS dict
# ---------------------------------------------------------------------------

ACTIONS = {
    "add-asset-category": add_asset_category,
    "list-asset-categories": list_asset_categories,
    "add-asset": add_asset,
    "update-asset": update_asset,
    "get-asset": get_asset,
    "list-assets": list_assets,
    "generate-depreciation-schedule": generate_depreciation_schedule,
    "post-depreciation": post_depreciation,
    "run-depreciation": run_depreciation,
    "record-asset-movement": record_asset_movement,
    "schedule-maintenance": schedule_maintenance,
    "complete-maintenance": complete_maintenance,
    "dispose-asset": dispose_asset,
    "asset-register-report": asset_register_report,
    "depreciation-summary": depreciation_summary,
    "status": status_action,
}


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ERPClaw Assets Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Entity IDs
    parser.add_argument("--company-id")
    parser.add_argument("--asset-id")
    parser.add_argument("--asset-category-id")
    parser.add_argument("--item-id")
    parser.add_argument("--depreciation-schedule-id")
    parser.add_argument("--maintenance-id")

    # Asset category fields
    parser.add_argument("--name")
    parser.add_argument("--depreciation-method")
    parser.add_argument("--useful-life-years")
    parser.add_argument("--asset-account-id")
    parser.add_argument("--depreciation-account-id")
    parser.add_argument("--accumulated-depreciation-account-id")

    # Asset fields
    parser.add_argument("--gross-value")
    parser.add_argument("--salvage-value")
    parser.add_argument("--purchase-date")
    parser.add_argument("--purchase-invoice-id")
    parser.add_argument("--depreciation-start-date")
    parser.add_argument("--location")
    parser.add_argument("--custodian-employee-id")
    parser.add_argument("--warranty-expiry-date")

    # Movement fields
    parser.add_argument("--movement-type")
    parser.add_argument("--movement-date")
    parser.add_argument("--from-location")
    parser.add_argument("--to-location")
    parser.add_argument("--from-employee-id")
    parser.add_argument("--to-employee-id")
    parser.add_argument("--reason")

    # Maintenance fields
    parser.add_argument("--maintenance-type")
    parser.add_argument("--scheduled-date")
    parser.add_argument("--actual-date")
    parser.add_argument("--cost")
    parser.add_argument("--performed-by")
    parser.add_argument("--description")
    parser.add_argument("--next-due-date")

    # Disposal fields
    parser.add_argument("--disposal-date")
    parser.add_argument("--disposal-method")
    parser.add_argument("--sale-amount")
    parser.add_argument("--buyer-details")

    # GL / posting fields
    parser.add_argument("--posting-date")
    parser.add_argument("--cost-center-id")

    # Report fields
    parser.add_argument("--as-of-date")

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
        sys.stderr.write(f"[erpclaw-assets] {e}\n")
        err("An unexpected error occurred")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
