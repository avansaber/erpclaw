#!/usr/bin/env python3
"""ERPClaw Billing Skill — db_query.py

Usage-based and metered billing: meters, readings, usage events, rate plans,
billing periods, bill runs, prepaid credits.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

# ---------------------------------------------------------------------------
# Shared library
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
    from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH  # noqa: E402
    from erpclaw_lib.decimal_utils import to_decimal, round_currency  # noqa: E402
    from erpclaw_lib.naming import get_next_name  # noqa: E402
    from erpclaw_lib.validation import check_input_lengths  # noqa: E402
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw-setup first: clawhub install erpclaw-setup", "suggestion": "clawhub install erpclaw-setup"}))
    sys.exit(1)

REQUIRED_TABLES = ["company"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VALID_SERVICE_TYPES = (
    "electricity", "water", "gas", "telecom", "saas",
    "parking", "rental", "waste", "custom",
)
VALID_METER_STATUSES = ("active", "disconnected", "removed", "suspended")
VALID_READING_TYPES = ("actual", "estimated", "adjusted", "rollover")
VALID_READING_SOURCES = ("manual", "smart_meter", "api", "import", "estimated")
VALID_PLAN_TYPES = (
    "flat", "tiered", "time_of_use", "demand",
    "volume_discount", "prepaid_credit", "hybrid",
)
VALID_SUPPORTED_PLAN_TYPES = ("flat", "tiered", "volume_discount")
VALID_BASE_CHARGE_PERIODS = ("monthly", "quarterly", "annually")
VALID_BILLING_PERIOD_STATUSES = (
    "open", "rated", "invoiced", "paid", "disputed", "void",
)
VALID_ADJUSTMENT_TYPES = (
    "credit", "late_fee", "deposit", "refund",
    "proration", "discount", "penalty", "write_off",
)
VALID_PREPAID_STATUSES = ("active", "exhausted", "expired")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_json_arg(value, name):
    if not value:
        err(f"--{name} is required")
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        err(f"--{name} must be valid JSON")


def _resolve_company_from_customer(conn, customer_id):
    row = conn.execute(
        "SELECT company_id FROM customer WHERE id = ?", (customer_id,)
    ).fetchone()
    if not row:
        err(f"Customer not found: {customer_id}",
             suggestion="Use 'list customers' in the selling skill to see available customers.")
    return row["company_id"]


# =========================================================================
# METERS (actions 1-4)
# =========================================================================

def add_meter(conn, args):
    """Register a new meter for a customer."""
    if not args.customer_id:
        err("--customer-id is required")
    if not args.meter_type:
        err("--meter-type is required")

    service_type = args.meter_type
    if service_type not in VALID_SERVICE_TYPES:
        err(f"Invalid meter-type: {service_type}. "
             f"Must be one of: {', '.join(VALID_SERVICE_TYPES)}")

    cust = conn.execute("SELECT id, company_id FROM customer WHERE id = ?",
                        (args.customer_id,)).fetchone()
    if not cust:
        err(f"Customer not found: {args.customer_id}")

    if args.rate_plan_id:
        rp = conn.execute("SELECT id FROM rate_plan WHERE id = ?",
                          (args.rate_plan_id,)).fetchone()
        if not rp:
            err(f"Rate plan not found: {args.rate_plan_id}")

    company_id = cust["company_id"]
    conn.company_id = company_id
    meter_number = get_next_name(conn, "meter")

    meter_id = str(uuid.uuid4())
    metadata = json.dumps({"uom": args.unit}) if args.unit else None

    conn.execute(
        """INSERT INTO meter (id, meter_number, customer_id, service_type,
           service_point_id, service_point_address, rate_plan_id, install_date,
           status, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
        (meter_id, meter_number, args.customer_id, service_type,
         args.name, args.address, args.rate_plan_id, args.install_date,
         metadata, _now(), _now()),
    )
    conn.commit()
    audit(conn, "erpclaw-billing", "add-meter", "meter", meter_id,
           new_values={"meter_number": meter_number, "service_type": service_type})

    meter = row_to_dict(conn.execute(
        "SELECT * FROM meter WHERE id = ?", (meter_id,)).fetchone())
    ok({"meter": meter})


def update_meter(conn, args):
    """Update meter configuration."""
    if not args.meter_id:
        err("--meter-id is required")

    meter = conn.execute("SELECT * FROM meter WHERE id = ?",
                         (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    updates, params, old_values = [], [], {}

    if args.name is not None:
        old_values["service_point_id"] = meter["service_point_id"]
        updates.append("service_point_id = ?")
        params.append(args.name)

    if args.status is not None:
        if args.status not in VALID_METER_STATUSES:
            err(f"Invalid status: {args.status}. "
                 f"Must be one of: {', '.join(VALID_METER_STATUSES)}")
        old_values["status"] = meter["status"]
        updates.append("status = ?")
        params.append(args.status)

    if args.rate_plan_id is not None:
        rp = conn.execute("SELECT id FROM rate_plan WHERE id = ?",
                          (args.rate_plan_id,)).fetchone()
        if not rp:
            err(f"Rate plan not found: {args.rate_plan_id}")
        old_values["rate_plan_id"] = meter["rate_plan_id"]
        updates.append("rate_plan_id = ?")
        params.append(args.rate_plan_id)

    if not updates:
        err("No fields to update. Provide --name, --status, or --rate-plan-id")

    updates.append("updated_at = ?")
    params.append(_now())
    params.append(args.meter_id)

    conn.execute(f"UPDATE meter SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    audit(conn, "erpclaw-billing", "update-meter", "meter", args.meter_id, old_values=old_values)

    meter = row_to_dict(conn.execute(
        "SELECT * FROM meter WHERE id = ?", (args.meter_id,)).fetchone())
    ok({"meter": meter})


def get_meter(conn, args):
    """Get meter with latest reading."""
    if not args.meter_id:
        err("--meter-id is required")

    meter = conn.execute("SELECT * FROM meter WHERE id = ?",
                         (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    result = row_to_dict(meter)

    latest = conn.execute(
        """SELECT * FROM meter_reading WHERE meter_id = ?
           ORDER BY reading_date DESC LIMIT 1""",
        (args.meter_id,)).fetchone()
    result["latest_reading"] = row_to_dict(latest) if latest else None

    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM meter_reading WHERE meter_id = ?",
        (args.meter_id,)).fetchone()
    result["reading_count"] = count["cnt"]
    ok({"meter": result})


def list_meters(conn, args):
    """List meters with optional filters."""
    where, params = [], []

    if args.customer_id:
        where.append("m.customer_id = ?")
        params.append(args.customer_id)
    if args.meter_type:
        where.append("m.service_type = ?")
        params.append(args.meter_type)
    if args.status:
        where.append("m.status = ?")
        params.append(args.status)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM meter m {where_clause}",
        params).fetchone()["cnt"]

    rows = conn.execute(
        f"""SELECT m.*, c.name as customer_name
            FROM meter m LEFT JOIN customer c ON m.customer_id = c.id
            {where_clause} ORDER BY m.created_at DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset]).fetchall()
    ok({"meters": [dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# =========================================================================
# METER READINGS (actions 5-6)
# =========================================================================

def add_meter_reading(conn, args):
    """Record a meter reading with auto-consumption calculation."""
    if not args.meter_id:
        err("--meter-id is required")
    if not args.reading_date:
        err("--reading-date is required")
    if not args.reading_value:
        err("--reading-value is required")

    meter = conn.execute("SELECT * FROM meter WHERE id = ?",
                         (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    reading_type = args.reading_type or "actual"
    if reading_type not in VALID_READING_TYPES:
        err(f"Invalid reading-type: {reading_type}. "
             f"Must be one of: {', '.join(VALID_READING_TYPES)}")

    source = args.source or "manual"
    if source not in VALID_READING_SOURCES:
        err(f"Invalid source: {source}. "
             f"Must be one of: {', '.join(VALID_READING_SOURCES)}")

    reading_value = to_decimal(args.reading_value)
    previous_reading_value = None
    consumption = None

    if meter["last_reading_value"] is not None:
        previous_reading_value = to_decimal(meter["last_reading_value"])
        diff = reading_value - previous_reading_value
        if diff < 0:
            consumption = reading_value
            if reading_type == "actual":
                reading_type = "rollover"
        else:
            consumption = diff

    # Resolve UOM
    uom = args.uom
    if not uom and meter["metadata"]:
        try:
            uom = json.loads(meter["metadata"]).get("uom")
        except (json.JSONDecodeError, TypeError):
            pass

    reading_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO meter_reading (id, meter_id, reading_date, reading_value,
           previous_reading_value, consumption, reading_type, uom, source,
           validated, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (reading_id, args.meter_id, args.reading_date,
         str(reading_value),
         str(previous_reading_value) if previous_reading_value is not None else None,
         str(consumption) if consumption is not None else None,
         reading_type, uom, source, _now()),
    )

    conn.execute(
        """UPDATE meter SET last_reading_date = ?, last_reading_value = ?,
           updated_at = ? WHERE id = ?""",
        (args.reading_date, str(reading_value), _now(), args.meter_id),
    )
    conn.commit()
    audit(conn, "erpclaw-billing", "add-meter-reading", "meter_reading", reading_id,
           new_values={"reading_value": str(reading_value),
                       "consumption": str(consumption) if consumption else None})

    reading = row_to_dict(conn.execute(
        "SELECT * FROM meter_reading WHERE id = ?", (reading_id,)).fetchone())
    ok({"reading": reading})


def list_meter_readings(conn, args):
    """List meter readings with optional date filters."""
    if not args.meter_id:
        err("--meter-id is required")

    where = ["mr.meter_id = ?"]
    params = [args.meter_id]

    if args.from_date:
        where.append("mr.reading_date >= ?")
        params.append(args.from_date)
    if args.to_date:
        where.append("mr.reading_date <= ?")
        params.append(args.to_date)

    limit = int(args.limit or 20)
    offset = int(args.offset or 0)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM meter_reading mr WHERE {' AND '.join(where)}",
        params).fetchone()["cnt"]

    rows = conn.execute(
        f"""SELECT mr.* FROM meter_reading mr
            WHERE {' AND '.join(where)}
            ORDER BY mr.reading_date DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset]).fetchall()
    ok({"readings": [dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# =========================================================================
# USAGE EVENTS (actions 7-8)
# =========================================================================

def add_usage_event(conn, args):
    """Record a single usage event."""
    if not args.meter_id:
        err("--meter-id is required")
    if not args.event_date:
        err("--event-date is required")
    if not args.quantity:
        err("--quantity is required")

    meter = conn.execute("SELECT id, customer_id FROM meter WHERE id = ?",
                         (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    if args.idempotency_key:
        existing = conn.execute(
            "SELECT id FROM usage_event WHERE idempotency_key = ?",
            (args.idempotency_key,)).fetchone()
        if existing:
            evt = row_to_dict(conn.execute(
                "SELECT * FROM usage_event WHERE id = ?",
                (existing["id"],)).fetchone())
            ok({"usage_event": evt, "deduplicated": True})

    event_id = str(uuid.uuid4())
    event_type = args.event_type or "usage"

    conn.execute(
        """INSERT INTO usage_event (id, customer_id, meter_id, event_type,
           quantity, timestamp, metadata, idempotency_key, processed, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (event_id, meter["customer_id"], args.meter_id, event_type,
         args.quantity, args.event_date, args.properties, args.idempotency_key,
         _now()),
    )
    conn.commit()
    audit(conn, "erpclaw-billing", "add-usage-event", "usage_event", event_id,
           new_values={"quantity": args.quantity, "event_type": event_type})

    evt = row_to_dict(conn.execute(
        "SELECT * FROM usage_event WHERE id = ?", (event_id,)).fetchone())
    ok({"usage_event": evt})


def add_usage_events_batch(conn, args):
    """Bulk ingest usage events."""
    events_data = _parse_json_arg(args.events, "events")
    if not isinstance(events_data, list):
        err("--events must be a JSON array")
    if not events_data:
        err("--events array is empty")

    inserted = 0
    duplicates = 0
    errors = []

    for i, evt in enumerate(events_data):
        meter_id = evt.get("meter_id")
        event_date = evt.get("event_date")
        quantity = evt.get("quantity")
        event_type = evt.get("event_type", "usage")
        idempotency_key = evt.get("idempotency_key")
        metadata = json.dumps(evt.get("properties")) if evt.get("properties") else None

        if not meter_id or not event_date or not quantity:
            errors.append({"index": i, "error": "Missing meter_id, event_date, or quantity"})
            continue

        meter = conn.execute("SELECT id, customer_id FROM meter WHERE id = ?",
                             (meter_id,)).fetchone()
        if not meter:
            errors.append({"index": i, "error": f"Meter not found: {meter_id}"})
            continue

        if idempotency_key:
            existing = conn.execute(
                "SELECT id FROM usage_event WHERE idempotency_key = ?",
                (idempotency_key,)).fetchone()
            if existing:
                duplicates += 1
                continue

        event_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO usage_event (id, customer_id, meter_id, event_type,
               quantity, timestamp, metadata, idempotency_key, processed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (event_id, meter["customer_id"], meter_id, event_type,
             str(quantity), event_date, metadata, idempotency_key, _now()),
        )
        inserted += 1

    conn.commit()
    ok({
        "inserted": inserted,
        "duplicates": duplicates,
        "errors": errors,
        "total_processed": len(events_data),
    })


# =========================================================================
# RATE PLANS (actions 9-12)
# =========================================================================

def add_rate_plan(conn, args):
    """Create a rate/pricing plan with optional tiers."""
    if not args.name:
        err("--name is required")
    if not args.billing_model:
        err("--billing-model is required")

    plan_type = args.billing_model
    if plan_type not in VALID_PLAN_TYPES:
        err(f"Invalid billing-model: {plan_type}. "
             f"Must be one of: {', '.join(VALID_PLAN_TYPES)}")

    effective_from = args.effective_from or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.base_charge_period and args.base_charge_period not in VALID_BASE_CHARGE_PERIODS:
        err(f"Invalid base-charge-period: {args.base_charge_period}. "
             f"Must be one of: {', '.join(VALID_BASE_CHARGE_PERIODS)}")

    plan_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO rate_plan (id, name, service_type, plan_type,
           base_charge, base_charge_period, currency, effective_from,
           effective_to, minimum_charge, minimum_commitment, overage_rate,
           metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, NULL, ?, ?)""",
        (plan_id, args.name, args.service_type, plan_type,
         args.base_charge, args.base_charge_period,
         effective_from, args.effective_to,
         args.minimum_charge, args.minimum_commitment, args.overage_rate,
         _now(), _now()),
    )

    if args.tiers:
        tiers_data = _parse_json_arg(args.tiers, "tiers")
        if not isinstance(tiers_data, list):
            err("--tiers must be a JSON array")
        for i, tier in enumerate(tiers_data):
            tier_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO rate_tier (id, rate_plan_id, tier_start, tier_end,
                   rate, fixed_charge, time_of_use_period, time_of_use_hours,
                   demand_type, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tier_id, plan_id,
                 tier.get("tier_start", "0"), tier.get("tier_end"),
                 tier.get("rate", "0"), tier.get("fixed_charge"),
                 tier.get("time_of_use_period"), tier.get("time_of_use_hours"),
                 tier.get("demand_type"), i),
            )

    conn.commit()
    audit(conn, "erpclaw-billing", "add-rate-plan", "rate_plan", plan_id,
           new_values={"name": args.name, "plan_type": plan_type})

    plan = row_to_dict(conn.execute(
        "SELECT * FROM rate_plan WHERE id = ?", (plan_id,)).fetchone())
    tiers = [dict(r) for r in conn.execute(
        "SELECT * FROM rate_tier WHERE rate_plan_id = ? ORDER BY sort_order",
        (plan_id,)).fetchall()]
    plan["tiers"] = tiers
    ok({"rate_plan": plan})


def update_rate_plan(conn, args):
    """Update rate plan configuration and/or tiers."""
    if not args.rate_plan_id:
        err("--rate-plan-id is required")

    plan = conn.execute("SELECT * FROM rate_plan WHERE id = ?",
                        (args.rate_plan_id,)).fetchone()
    if not plan:
        err(f"Rate plan not found: {args.rate_plan_id}")

    updates, params, old_values = [], [], {}

    for field, col in [("name", "name"), ("base_charge", "base_charge"),
                       ("effective_to", "effective_to"),
                       ("minimum_charge", "minimum_charge"),
                       ("overage_rate", "overage_rate")]:
        val = getattr(args, field, None)
        if val is not None:
            old_values[col] = plan[col]
            updates.append(f"{col} = ?")
            params.append(val)

    if updates:
        updates.append("updated_at = ?")
        params.append(_now())
        params.append(args.rate_plan_id)
        conn.execute(
            f"UPDATE rate_plan SET {', '.join(updates)} WHERE id = ?", params)

    if args.tiers:
        tiers_data = _parse_json_arg(args.tiers, "tiers")
        if not isinstance(tiers_data, list):
            err("--tiers must be a JSON array")
        conn.execute("DELETE FROM rate_tier WHERE rate_plan_id = ?",
                     (args.rate_plan_id,))
        for i, tier in enumerate(tiers_data):
            tier_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO rate_tier (id, rate_plan_id, tier_start, tier_end,
                   rate, fixed_charge, time_of_use_period, time_of_use_hours,
                   demand_type, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tier_id, args.rate_plan_id,
                 tier.get("tier_start", "0"), tier.get("tier_end"),
                 tier.get("rate", "0"), tier.get("fixed_charge"),
                 tier.get("time_of_use_period"), tier.get("time_of_use_hours"),
                 tier.get("demand_type"), i),
            )

    if not updates and not args.tiers:
        err("No fields to update")

    conn.commit()
    audit(conn, "erpclaw-billing", "update-rate-plan", "rate_plan", args.rate_plan_id,
           old_values=old_values)

    plan = row_to_dict(conn.execute(
        "SELECT * FROM rate_plan WHERE id = ?", (args.rate_plan_id,)).fetchone())
    tiers = [dict(r) for r in conn.execute(
        "SELECT * FROM rate_tier WHERE rate_plan_id = ? ORDER BY sort_order",
        (args.rate_plan_id,)).fetchall()]
    plan["tiers"] = tiers
    ok({"rate_plan": plan})


def get_rate_plan(conn, args):
    """Get rate plan with tiers."""
    if not args.rate_plan_id:
        err("--rate-plan-id is required")

    plan = conn.execute("SELECT * FROM rate_plan WHERE id = ?",
                        (args.rate_plan_id,)).fetchone()
    if not plan:
        err(f"Rate plan not found: {args.rate_plan_id}")

    result = row_to_dict(plan)
    tiers = [dict(r) for r in conn.execute(
        "SELECT * FROM rate_tier WHERE rate_plan_id = ? ORDER BY sort_order",
        (args.rate_plan_id,)).fetchall()]
    result["tiers"] = tiers
    ok({"rate_plan": result})


def list_rate_plans(conn, args):
    """List rate plans with optional filters."""
    where, params = [], []
    if args.service_type:
        where.append("service_type = ?")
        params.append(args.service_type)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM rate_plan {where_clause}",
        params).fetchone()["cnt"]

    rows = conn.execute(
        f"""SELECT * FROM rate_plan {where_clause}
            ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        params + [limit, offset]).fetchall()
    ok({"rate_plans": [dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# Internal: rate calculation engine
# ---------------------------------------------------------------------------

def _calculate_charge(plan_type, tiers, consumption, base_charge="0",
                      minimum_charge=None):
    """Pure function: calculate charges for a given consumption amount."""
    consumption = to_decimal(consumption)
    base = to_decimal(base_charge or "0")
    breakdown = []

    if plan_type == "flat":
        if not tiers:
            err("Flat rate plan requires at least one tier")
        rate = to_decimal(tiers[0].get("rate", "0"))
        usage_charge = round_currency(consumption * rate)
        breakdown.append({
            "tier": "flat", "consumption": str(consumption),
            "rate": str(rate), "charge": str(usage_charge),
        })

    elif plan_type == "tiered":
        usage_charge = Decimal("0")
        remaining = consumption
        sorted_tiers = sorted(tiers,
                              key=lambda t: to_decimal(t.get("tier_start", "0")))
        for tier in sorted_tiers:
            if remaining <= 0:
                break
            tier_start = to_decimal(tier.get("tier_start", "0"))
            tier_end_val = tier.get("tier_end")
            tier_end = to_decimal(tier_end_val) if tier_end_val else None
            rate = to_decimal(tier.get("rate", "0"))

            band_width = (tier_end - tier_start) if tier_end else remaining
            applicable = min(remaining, band_width)
            charge = round_currency(applicable * rate)
            usage_charge += charge
            remaining -= applicable
            breakdown.append({
                "tier_start": str(tier_start),
                "tier_end": str(tier_end) if tier_end else None,
                "consumption": str(applicable),
                "rate": str(rate), "charge": str(charge),
            })

    elif plan_type == "volume_discount":
        applicable_rate = Decimal("0")
        matched_tier = None
        sorted_tiers = sorted(tiers,
                              key=lambda t: to_decimal(t.get("tier_start", "0")))
        for tier in sorted_tiers:
            tier_start = to_decimal(tier.get("tier_start", "0"))
            tier_end_val = tier.get("tier_end")
            tier_end = to_decimal(tier_end_val) if tier_end_val else None
            if consumption >= tier_start and (tier_end is None or consumption < tier_end):
                applicable_rate = to_decimal(tier.get("rate", "0"))
                matched_tier = tier
                break

        usage_charge = round_currency(consumption * applicable_rate)
        breakdown.append({
            "tier": "volume_discount", "consumption": str(consumption),
            "rate": str(applicable_rate), "charge": str(usage_charge),
            "matched_tier_start": str(to_decimal(
                matched_tier.get("tier_start", "0"))) if matched_tier else None,
        })
    else:
        err(f"Rating for plan_type '{plan_type}' is not yet supported. "
             f"Supported: flat, tiered, volume_discount")

    total = round_currency(base + usage_charge)
    if minimum_charge:
        min_charge = to_decimal(minimum_charge)
        if total < min_charge:
            total = min_charge

    return {
        "usage_charge": str(usage_charge),
        "base_charge": str(base),
        "total_charge": str(total),
        "breakdown": breakdown,
    }


def rate_consumption(conn, args):
    """Pure function: calculate charges for consumption against a rate plan."""
    if not args.rate_plan_id:
        err("--rate-plan-id is required")
    if not args.consumption:
        err("--consumption is required")

    plan = conn.execute("SELECT * FROM rate_plan WHERE id = ?",
                        (args.rate_plan_id,)).fetchone()
    if not plan:
        err(f"Rate plan not found: {args.rate_plan_id}")

    plan_type = plan["plan_type"]
    if plan_type not in VALID_SUPPORTED_PLAN_TYPES:
        err(f"Rating for plan_type '{plan_type}' is not yet supported. "
             f"Supported: {', '.join(VALID_SUPPORTED_PLAN_TYPES)}")

    tiers = [dict(r) for r in conn.execute(
        "SELECT * FROM rate_tier WHERE rate_plan_id = ? ORDER BY sort_order",
        (args.rate_plan_id,)).fetchall()]

    result = _calculate_charge(
        plan_type, tiers, args.consumption,
        base_charge=plan["base_charge"],
        minimum_charge=plan["minimum_charge"],
    )
    result["rate_plan_name"] = plan["name"]
    result["plan_type"] = plan_type
    result["consumption"] = args.consumption
    ok({"calculation": result})


# =========================================================================
# BILLING PERIODS (actions 14-19)
# =========================================================================

def create_billing_period(conn, args):
    """Create a billing period for a customer/meter."""
    if not args.customer_id:
        err("--customer-id is required")
    if not args.meter_id:
        err("--meter-id is required")
    if not args.from_date:
        err("--from-date is required")
    if not args.to_date:
        err("--to-date is required")

    cust = conn.execute("SELECT id FROM customer WHERE id = ?",
                        (args.customer_id,)).fetchone()
    if not cust:
        err(f"Customer not found: {args.customer_id}")

    meter = conn.execute("SELECT * FROM meter WHERE id = ?",
                         (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    rate_plan_id = args.rate_plan_id or meter["rate_plan_id"]
    if not rate_plan_id:
        err("No rate plan specified. Provide --rate-plan-id or assign one to the meter")

    rp = conn.execute("SELECT id FROM rate_plan WHERE id = ?",
                      (rate_plan_id,)).fetchone()
    if not rp:
        err(f"Rate plan not found: {rate_plan_id}")

    # Check for overlapping period
    overlap = conn.execute(
        """SELECT id FROM billing_period
           WHERE meter_id = ? AND status != 'void'
           AND period_start <= ? AND period_end >= ?""",
        (args.meter_id, args.to_date, args.from_date)).fetchone()
    if overlap:
        err(f"Overlapping billing period exists: {overlap['id']}")

    period_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO billing_period (id, customer_id, meter_id, rate_plan_id,
           period_start, period_end, total_consumption, base_charge, usage_charge,
           adjustments_total, subtotal, tax_amount, grand_total,
           status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, '0', '0', '0', '0', '0', '0', '0',
                   'open', ?, ?)""",
        (period_id, args.customer_id, args.meter_id, rate_plan_id,
         args.from_date, args.to_date, _now(), _now()),
    )
    conn.commit()
    audit(conn, "erpclaw-billing", "create-billing-period", "billing_period", period_id,
           new_values={"period_start": args.from_date, "period_end": args.to_date})

    bp = row_to_dict(conn.execute(
        "SELECT * FROM billing_period WHERE id = ?", (period_id,)).fetchone())
    ok({"billing_period": bp})


def run_billing(conn, args):
    """Execute bill run: aggregate usage, rate consumption, create periods."""
    if not args.company_id:
        err("--company-id is required")
    if not args.billing_date:
        err("--billing-date is required")

    company = conn.execute("SELECT id FROM company WHERE id = ?",
                           (args.company_id,)).fetchone()
    if not company:
        err(f"Company not found: {args.company_id}")

    billing_date = args.billing_date
    from_date = args.from_date
    to_date = args.to_date or billing_date

    if not from_date:
        bd = datetime.strptime(billing_date, "%Y-%m-%d")
        from_date = (bd - timedelta(days=30)).strftime("%Y-%m-%d")

    # Find all customers for this company
    customers = conn.execute(
        "SELECT id FROM customer WHERE company_id = ?",
        (args.company_id,)).fetchall()
    if not customers:
        ok({"periods_created": 0, "total_billed": "0.00",
             "message": "No customers found for this company"})

    customer_ids = [c["id"] for c in customers]
    placeholders = ",".join("?" * len(customer_ids))

    # Find active meters with rate plans
    meters = conn.execute(
        f"""SELECT m.* FROM meter m
            WHERE m.customer_id IN ({placeholders})
            AND m.status = 'active' AND m.rate_plan_id IS NOT NULL""",
        customer_ids).fetchall()

    if not meters:
        ok({"periods_created": 0, "total_billed": "0.00",
             "message": "No active meters with rate plans found"})

    created_periods = []
    total_billed = Decimal("0")

    for meter in meters:
        meter_id = meter["id"]
        rate_plan_id = meter["rate_plan_id"]

        # Skip if already billed for this period
        existing = conn.execute(
            """SELECT id, status FROM billing_period
               WHERE meter_id = ? AND period_start = ? AND period_end = ?
               AND status NOT IN ('void')""",
            (meter_id, from_date, to_date)).fetchone()
        if existing and existing["status"] in ("rated", "invoiced", "paid"):
            continue

        # Load rate plan + tiers
        plan = conn.execute("SELECT * FROM rate_plan WHERE id = ?",
                            (rate_plan_id,)).fetchone()
        if not plan:
            continue

        plan_type = plan["plan_type"]
        if plan_type not in VALID_SUPPORTED_PLAN_TYPES:
            continue

        tiers = [dict(r) for r in conn.execute(
            "SELECT * FROM rate_tier WHERE rate_plan_id = ? ORDER BY sort_order",
            (rate_plan_id,)).fetchall()]

        # Aggregate consumption from readings
        readings_row = conn.execute(
            """SELECT COALESCE(SUM(consumption), 0) as total
               FROM meter_reading
               WHERE meter_id = ? AND reading_date >= ? AND reading_date <= ?
               AND consumption IS NOT NULL""",
            (meter_id, from_date, to_date)).fetchone()
        readings_consumption = to_decimal(str(readings_row["total"]))

        # Aggregate from unprocessed usage events
        events_row = conn.execute(
            """SELECT COALESCE(SUM(quantity), 0) as total
               FROM usage_event
               WHERE meter_id = ? AND processed = 0
               AND timestamp >= ? AND timestamp <= ?""",
            (meter_id, from_date, to_date)).fetchone()
        events_consumption = to_decimal(str(events_row["total"]))

        total_consumption = readings_consumption + events_consumption

        # Rate the consumption
        charges = _calculate_charge(
            plan_type, tiers, str(total_consumption),
            base_charge=plan["base_charge"],
            minimum_charge=plan["minimum_charge"],
        )

        usage_charge = charges["usage_charge"]
        base_charge = charges["base_charge"]
        total_charge = charges["total_charge"]
        subtotal = total_charge  # Before adjustments

        # Create or update billing period
        if existing and existing["status"] == "open":
            period_id = existing["id"]
            conn.execute(
                """UPDATE billing_period SET
                   total_consumption = ?, base_charge = ?, usage_charge = ?,
                   subtotal = ?, grand_total = ?, status = 'rated',
                   rated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (str(total_consumption), base_charge, usage_charge,
                 subtotal, total_charge, _now(), _now(), period_id),
            )
        else:
            period_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO billing_period (id, customer_id, meter_id,
                   rate_plan_id, period_start, period_end,
                   total_consumption, base_charge, usage_charge,
                   adjustments_total, subtotal, tax_amount, grand_total,
                   status, rated_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '0', ?, '0', ?,
                           'rated', ?, ?, ?)""",
                (period_id, meter["customer_id"], meter_id, rate_plan_id,
                 from_date, to_date, str(total_consumption),
                 base_charge, usage_charge, subtotal, total_charge,
                 _now(), _now(), _now()),
            )

        # Mark usage events as processed
        conn.execute(
            """UPDATE usage_event SET processed = 1, billing_period_id = ?
               WHERE meter_id = ? AND processed = 0
               AND timestamp >= ? AND timestamp <= ?""",
            (period_id, meter_id, from_date, to_date),
        )

        total_billed += to_decimal(total_charge)
        created_periods.append(period_id)

    conn.commit()
    audit(conn, "erpclaw-billing", "run-billing", "billing_period", ",".join(created_periods),
           new_values={"periods": len(created_periods),
                       "total_billed": str(round_currency(total_billed))})

    ok({
        "periods_created": len(created_periods),
        "period_ids": created_periods,
        "total_billed": str(round_currency(total_billed)),
    })


def generate_invoices(conn, args):
    """Create sales invoices from rated billing periods."""
    bp_ids = _parse_json_arg(args.billing_period_ids, "billing-period-ids")
    if not isinstance(bp_ids, list):
        err("--billing-period-ids must be a JSON array")

    # Try to find selling script (optional — invoice creation skipped if unavailable)
    from erpclaw_lib.dependencies import resolve_skill_script, table_exists
    selling_script = resolve_skill_script("erpclaw-selling") if table_exists(conn, "sales_invoice") else None

    results = []
    for bp_id in bp_ids:
        bp = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
        if not bp:
            results.append({"billing_period_id": bp_id, "error": "Not found"})
            continue
        if bp["status"] != "rated":
            results.append({"billing_period_id": bp_id,
                            "error": f"Status is '{bp['status']}', expected 'rated'"})
            continue

        invoice_id = None
        if selling_script:
            try:
                cmd = [
                    "python3", selling_script,
                    "--action", "add-sales-invoice",
                    "--customer-id", bp["customer_id"],
                    "--items", json.dumps([{
                        "description": (f"Billing period "
                                        f"{bp['period_start']} to {bp['period_end']}"),
                        "qty": "1",
                        "rate": bp["grand_total"],
                    }]),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if proc.returncode == 0:
                    inv_result = json.loads(proc.stdout)
                    if inv_result.get("status") == "ok":
                        invoice_id = inv_result.get("sales_invoice", {}).get("id")
            except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
                pass

        conn.execute(
            """UPDATE billing_period SET status = 'invoiced',
               invoiced_at = ?, invoice_id = ?, updated_at = ?
               WHERE id = ?""",
            (_now(), invoice_id, _now(), bp_id),
        )
        results.append({
            "billing_period_id": bp_id,
            "invoice_id": invoice_id,
            "status": "invoiced",
        })

    conn.commit()
    invoiced_count = sum(1 for r in results if r.get("status") == "invoiced")
    ok({"invoiced": invoiced_count, "results": results})


def add_billing_adjustment(conn, args):
    """Add an adjustment (credit, late fee, etc.) to a billing period."""
    if not args.billing_period_id:
        err("--billing-period-id is required")
    if not args.amount:
        err("--amount is required")
    if not args.adjustment_type:
        err("--adjustment-type is required")

    if args.adjustment_type not in VALID_ADJUSTMENT_TYPES:
        err(f"Invalid adjustment-type: {args.adjustment_type}. "
             f"Must be one of: {', '.join(VALID_ADJUSTMENT_TYPES)}")

    bp = conn.execute("SELECT * FROM billing_period WHERE id = ?",
                      (args.billing_period_id,)).fetchone()
    if not bp:
        err(f"Billing period not found: {args.billing_period_id}")

    adj_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO billing_adjustment (id, billing_period_id, adjustment_type,
           amount, reason, approved_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (adj_id, args.billing_period_id, args.adjustment_type,
         args.amount, args.reason, args.approved_by, _now()),
    )

    # Recalculate totals
    adj_total_row = conn.execute(
        """SELECT COALESCE(decimal_sum(amount), '0') as total
           FROM billing_adjustment WHERE billing_period_id = ?""",
        (args.billing_period_id,)).fetchone()
    adj_total = round_currency(to_decimal(str(adj_total_row["total"])))

    base = to_decimal(bp["base_charge"] or "0")
    usage = to_decimal(bp["usage_charge"] or "0")
    tax = to_decimal(bp["tax_amount"] or "0")
    subtotal = round_currency(base + usage + adj_total)
    grand_total = round_currency(subtotal + tax)

    conn.execute(
        """UPDATE billing_period SET adjustments_total = ?,
           subtotal = ?, grand_total = ?, updated_at = ?
           WHERE id = ?""",
        (str(adj_total), str(subtotal), str(grand_total),
         _now(), args.billing_period_id),
    )
    conn.commit()

    audit(conn, "erpclaw-billing", "add-billing-adjustment", "billing_adjustment", adj_id,
           new_values={"amount": args.amount, "type": args.adjustment_type})

    adj = row_to_dict(conn.execute(
        "SELECT * FROM billing_adjustment WHERE id = ?", (adj_id,)).fetchone())
    adj["updated_grand_total"] = str(grand_total)
    ok({"adjustment": adj})


def list_billing_periods(conn, args):
    """List billing periods with optional filters."""
    where, params = [], []

    if args.customer_id:
        where.append("bp.customer_id = ?")
        params.append(args.customer_id)
    if args.meter_id:
        where.append("bp.meter_id = ?")
        params.append(args.meter_id)
    if args.status:
        where.append("bp.status = ?")
        params.append(args.status)
    if args.from_date:
        where.append("bp.period_start >= ?")
        params.append(args.from_date)
    if args.to_date:
        where.append("bp.period_end <= ?")
        params.append(args.to_date)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM billing_period bp {where_clause}",
        params).fetchone()["cnt"]

    rows = conn.execute(
        f"""SELECT bp.*, c.name as customer_name, m.meter_number
            FROM billing_period bp
            LEFT JOIN customer c ON bp.customer_id = c.id
            LEFT JOIN meter m ON bp.meter_id = m.id
            {where_clause}
            ORDER BY bp.created_at DESC LIMIT ? OFFSET ?""",
        params + [limit, offset]).fetchall()
    ok({"billing_periods": [dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


def get_billing_period(conn, args):
    """Get billing period with adjustments."""
    if not args.billing_period_id:
        err("--billing-period-id is required")

    bp = conn.execute(
        """SELECT bp.*, c.name as customer_name, m.meter_number,
                  rp.name as rate_plan_name
           FROM billing_period bp
           LEFT JOIN customer c ON bp.customer_id = c.id
           LEFT JOIN meter m ON bp.meter_id = m.id
           LEFT JOIN rate_plan rp ON bp.rate_plan_id = rp.id
           WHERE bp.id = ?""",
        (args.billing_period_id,)).fetchone()
    if not bp:
        err(f"Billing period not found: {args.billing_period_id}")

    result = row_to_dict(bp)
    adjustments = [dict(r) for r in conn.execute(
        """SELECT * FROM billing_adjustment
           WHERE billing_period_id = ? ORDER BY created_at""",
        (args.billing_period_id,)).fetchall()]
    result["adjustments"] = adjustments
    ok({"billing_period": result})


# =========================================================================
# PREPAID CREDITS (actions 20-21)
# =========================================================================

def add_prepaid_credit(conn, args):
    """Record a prepaid commitment."""
    if not args.customer_id:
        err("--customer-id is required")
    if not args.amount:
        err("--amount is required")
    if not args.valid_until:
        err("--valid-until is required")

    cust = conn.execute("SELECT id FROM customer WHERE id = ?",
                        (args.customer_id,)).fetchone()
    if not cust:
        err(f"Customer not found: {args.customer_id}")

    rate_plan_id = args.rate_plan_id
    if rate_plan_id:
        rp = conn.execute("SELECT id FROM rate_plan WHERE id = ?",
                          (rate_plan_id,)).fetchone()
        if not rp:
            err(f"Rate plan not found: {rate_plan_id}")
    else:
        # Find any rate plan with type prepaid_credit, or use first available
        rp = conn.execute(
            "SELECT id FROM rate_plan WHERE plan_type = 'prepaid_credit' LIMIT 1"
        ).fetchone()
        if not rp:
            rp = conn.execute("SELECT id FROM rate_plan LIMIT 1").fetchone()
        if not rp:
            err("No rate plan available. Create one first")
        rate_plan_id = rp["id"]

    period_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    amount = args.amount

    credit_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO prepaid_credit_balance (id, customer_id, rate_plan_id,
           original_amount, remaining_amount, period_start, period_end,
           overage_amount, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, '0', 'active', ?, ?)""",
        (credit_id, args.customer_id, rate_plan_id,
         amount, amount, period_start, args.valid_until,
         _now(), _now()),
    )
    conn.commit()
    audit(conn, "erpclaw-billing", "add-prepaid-credit", "prepaid_credit_balance", credit_id,
           new_values={"amount": amount, "valid_until": args.valid_until})

    credit = row_to_dict(conn.execute(
        "SELECT * FROM prepaid_credit_balance WHERE id = ?",
        (credit_id,)).fetchone())
    ok({"prepaid_credit": credit})


def get_prepaid_balance(conn, args):
    """Check remaining prepaid credits for a customer."""
    if not args.customer_id:
        err("--customer-id is required")

    rows = conn.execute(
        """SELECT * FROM prepaid_credit_balance
           WHERE customer_id = ? ORDER BY created_at DESC""",
        (args.customer_id,)).fetchall()

    balances = [dict(r) for r in rows]
    total_remaining = Decimal("0")
    active_count = 0
    for b in balances:
        if b["status"] == "active":
            total_remaining += to_decimal(b["remaining_amount"] or "0")
            active_count += 1

    ok({
        "customer_id": args.customer_id,
        "active_credits": active_count,
        "total_remaining": str(round_currency(total_remaining)),
        "balances": balances,
    })


# =========================================================================
# STATUS (action 22)
# =========================================================================

def status_action(conn, args):
    """Billing summary."""
    where = ""
    params = []
    cust_where = ""

    if args.company_id:
        company = conn.execute("SELECT id FROM company WHERE id = ?",
                               (args.company_id,)).fetchone()
        if not company:
            err(f"Company not found: {args.company_id}")
        cust_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM customer WHERE company_id = ?",
            (args.company_id,)).fetchall()]
        if cust_ids:
            placeholders = ",".join("?" * len(cust_ids))
            where = f"WHERE customer_id IN ({placeholders})"
            params = cust_ids
            cust_where = where

    # Meter counts by status
    meter_q = f"""SELECT status, COUNT(*) as cnt FROM meter
                  {where.replace('customer_id', 'customer_id')}
                  GROUP BY status""" if not where else \
              f"""SELECT status, COUNT(*) as cnt FROM meter
                  {where} GROUP BY status"""
    meter_counts = {}
    for row in conn.execute(meter_q, params).fetchall():
        meter_counts[row["status"]] = row["cnt"]

    # Billing period counts by status
    bp_q = f"SELECT status, COUNT(*) as cnt FROM billing_period {cust_where} GROUP BY status"
    bp_counts = {}
    for row in conn.execute(bp_q, params).fetchall():
        bp_counts[row["status"]] = row["cnt"]

    # Unprocessed usage events
    evt_q = f"""SELECT COUNT(*) as cnt FROM usage_event
                WHERE processed = 0"""
    if params:
        evt_q += f" AND customer_id IN ({','.join('?' * len(params))})"
    unprocessed = conn.execute(evt_q, params).fetchone()["cnt"]

    # Rate plans count
    rp_count = conn.execute("SELECT COUNT(*) as cnt FROM rate_plan").fetchone()["cnt"]

    # Prepaid balance count
    prepaid_q = f"SELECT COUNT(*) as cnt FROM prepaid_credit_balance {cust_where}"
    prepaid_count = conn.execute(prepaid_q, params).fetchone()["cnt"]

    ok({
        "meters": meter_counts,
        "meters_total": sum(meter_counts.values()),
        "billing_periods": bp_counts,
        "billing_periods_total": sum(bp_counts.values()),
        "rate_plans_total": rp_count,
        "unprocessed_events": unprocessed,
        "prepaid_balances": prepaid_count,
    })


# =========================================================================
# Action registry + main
# =========================================================================

ACTIONS = {
    "add-meter": add_meter,
    "update-meter": update_meter,
    "get-meter": get_meter,
    "list-meters": list_meters,
    "add-meter-reading": add_meter_reading,
    "list-meter-readings": list_meter_readings,
    "add-usage-event": add_usage_event,
    "add-usage-events-batch": add_usage_events_batch,
    "add-rate-plan": add_rate_plan,
    "update-rate-plan": update_rate_plan,
    "get-rate-plan": get_rate_plan,
    "list-rate-plans": list_rate_plans,
    "rate-consumption": rate_consumption,
    "create-billing-period": create_billing_period,
    "run-billing": run_billing,
    "generate-invoices": generate_invoices,
    "add-billing-adjustment": add_billing_adjustment,
    "list-billing-periods": list_billing_periods,
    "get-billing-period": get_billing_period,
    "add-prepaid-credit": add_prepaid_credit,
    "get-prepaid-balance": get_prepaid_balance,
    "status": status_action,
}


def main():
    parser = argparse.ArgumentParser(description="ERPClaw Billing")
    parser.add_argument("--action", required=True, choices=list(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)
    # Entity IDs
    parser.add_argument("--meter-id")
    parser.add_argument("--rate-plan-id")
    parser.add_argument("--billing-period-id")
    parser.add_argument("--customer-id")
    parser.add_argument("--company-id")
    parser.add_argument("--item-id")
    parser.add_argument("--serial-number-id")
    # Meter fields
    parser.add_argument("--name")
    parser.add_argument("--meter-type")
    parser.add_argument("--unit")
    parser.add_argument("--install-date")
    parser.add_argument("--address")
    parser.add_argument("--status")
    # Reading fields
    parser.add_argument("--reading-date")
    parser.add_argument("--reading-value")
    parser.add_argument("--reading-type")
    parser.add_argument("--source")
    parser.add_argument("--uom")
    parser.add_argument("--estimated-reason")
    # Usage event fields
    parser.add_argument("--event-date")
    parser.add_argument("--event-type")
    parser.add_argument("--quantity")
    parser.add_argument("--properties")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--events")
    # Rate plan fields
    parser.add_argument("--billing-model")
    parser.add_argument("--tiers")
    parser.add_argument("--base-charge")
    parser.add_argument("--base-charge-period")
    parser.add_argument("--effective-from")
    parser.add_argument("--effective-to")
    parser.add_argument("--minimum-charge")
    parser.add_argument("--minimum-commitment")
    parser.add_argument("--overage-rate")
    parser.add_argument("--service-type")
    parser.add_argument("--consumption")
    # Billing fields
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--billing-date")
    parser.add_argument("--billing-period-ids")
    # Adjustment fields
    parser.add_argument("--amount")
    parser.add_argument("--adjustment-type")
    parser.add_argument("--reason")
    parser.add_argument("--approved-by")
    # Prepaid fields
    parser.add_argument("--valid-until")
    # Filters
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")

    args, _unknown = parser.parse_known_args()
    check_input_lengths(args)

    db_path = args.db_path
    if db_path:
        os.environ["ERPCLAW_DB_PATH"] = db_path

    ensure_db_exists()
    conn = get_connection()

    # Dependency check
    _dep = check_required_tables(conn, REQUIRED_TABLES)
    if _dep:
        _dep["suggestion"] = "clawhub install " + " ".join(_dep.get("missing_skills", []))
        print(json.dumps(_dep, indent=2))
        conn.close()
        sys.exit(1)

    try:
        ACTIONS[args.action](conn, args)
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"[erpclaw-billing] {e}\n")
        err("An unexpected error occurred")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
