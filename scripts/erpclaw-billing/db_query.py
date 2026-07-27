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
    sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH  # noqa: E402
    from erpclaw_lib.decimal_utils import to_decimal, round_currency  # noqa: E402
    from erpclaw_lib.naming import get_next_name  # noqa: E402
    from erpclaw_lib.validation import check_input_lengths  # noqa: E402
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
    from erpclaw_lib.query import Q, P, Table, Field, fn, Order, DecimalSum
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args
    from erpclaw_lib.vendor.pypika.terms import ValueWrapper
    from erpclaw_lib import billing_run as billing_run_lib
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw first: clawhub install erpclaw", "suggestion": "clawhub install erpclaw"}))
    sys.exit(1)

REQUIRED_TABLES = ["company"]

# ---------------------------------------------------------------------------
# PyPika table aliases
# ---------------------------------------------------------------------------
T_meter = Table("meter")
T_meter_reading = Table("meter_reading")
T_usage_event = Table("usage_event")
T_rate_plan = Table("rate_plan")
T_rate_tier = Table("rate_tier")
T_billing_period = Table("billing_period")
T_billing_adjustment = Table("billing_adjustment")
T_prepaid = Table("prepaid_credit_balance")
T_customer = Table("customer")
T_company = Table("company")

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
# All 7 plan types are implemented by the rating engine (Wave F S1.1,
# 2026-07-25). The symbol is kept because callers gate on it by name; it is
# now identical to VALID_PLAN_TYPES — an unknown plan_type in the DB is a
# data error and fails loud everywhere (never a silent skip).
VALID_SUPPORTED_PLAN_TYPES = VALID_PLAN_TYPES
VALID_TOU_PERIODS = ("peak", "off_peak", "shoulder")
# Plan types composable inside a hybrid tier_strategy. prepaid_credit is
# deliberately excluded: it is a settlement mechanism (credit deduction),
# not a rate shape — composing it would deduct the balance twice.
HYBRID_COMPONENT_TYPES = ("flat", "tiered", "volume_discount",
                          "time_of_use", "demand")
MINUTES_PER_DAY = 24 * 60
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


class _UnrepresentableAmount(ValueError):
    """A finite Decimal whose magnitude cannot survive round_currency.

    Subclasses ValueError so every existing (TypeError, ValueError,
    ArithmeticError) handler covers it exactly like unparseable garbage;
    callers that want the distinct "representable amount" wording catch
    this class first.
    """


def _finite_decimal(value):
    """to_decimal that guarantees ROUND-TRIP SURVIVABILITY, not just
    finiteness.

    Two rejection legs (both ride the ValueError family so every existing
    (TypeError, ValueError, ArithmeticError) handler covers them):

    1. Non-finite: 'NaN'/'sNaN'/'Infinity'/'-Infinity' (any case) PARSE as
       Decimal but poison every sum/comparison they touch (QA round-2
       DEFECT-1). Plain ValueError, same message class as garbage strings.
    2. Finite-but-unrepresentable (QA round-3): a plain 27+-integer-digit
       magnitude passes is_finite() but round_currency's
       quantize(Decimal('0.01')) raises decimal.InvalidOperation under the
       default 28-digit context — an ArithmeticError, NOT a RatingError,
       which escaped the per-meter guard and aborted whole billing runs.
       The probe-quantize below is the SAME operation round_currency
       performs, so the gate invariant holds by construction: ANY value
       that passes this gate survives round_currency without raising.
       Raises _UnrepresentableAmount (a ValueError).
    """
    result = to_decimal(value)
    if not result.is_finite():
        raise ValueError(f"non-finite number {value!r}")
    try:
        result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except ArithmeticError:
        raise _UnrepresentableAmount(
            f"unrepresentable magnitude {value!r}")
    return result


def _check_number(value, label):
    """Validate a money/quantity CLI argument parses as a usable Decimal.

    Write-time gate (QA bounce D3 root cause + round-2 DEFECT-1 + round-3
    representability): garbage numbers — unparseable strings, non-finite
    values like 'NaN'/'Infinity', AND finite-but-unrepresentable magnitudes
    — must never enter the DB, where they would later surface as
    rating-time data errors or abort whole billing runs.
    err() exits with a named message; returns the Decimal.
    """
    try:
        return _finite_decimal(value)
    except _UnrepresentableAmount:
        err(f"{label} must be a representable amount, got: {value!r} "
             f"(magnitude exceeds supported currency precision)")
    except (TypeError, ValueError, ArithmeticError):
        err(f"{label} must be a number, got: {value!r}")


def _resolve_company_from_customer(conn, customer_id):
    q = Q.from_(T_customer).select(T_customer.company_id).where(T_customer.id == P())
    row = conn.execute(q.get_sql(), (customer_id,)).fetchone()
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

    q = Q.from_(T_customer).select(T_customer.id, T_customer.company_id).where(T_customer.id == P())
    cust = conn.execute(q.get_sql(), (args.customer_id,)).fetchone()
    if not cust:
        err(f"Customer not found: {args.customer_id}")

    if args.rate_plan_id:
        q = Q.from_(T_rate_plan).select(T_rate_plan.id).where(T_rate_plan.id == P())
        rp = conn.execute(q.get_sql(), (args.rate_plan_id,)).fetchone()
        if not rp:
            err(f"Rate plan not found: {args.rate_plan_id}")

    company_id = cust["company_id"]
    conn.company_id = company_id
    meter_number = get_next_name(conn, "meter")

    meter_id = str(uuid.uuid4())
    metadata = json.dumps({"uom": args.unit}) if args.unit else None
    now = _now()

    q = (Q.into(T_meter)
         .columns("id", "meter_number", "customer_id", "service_type",
                  "service_point_id", "service_point_address", "rate_plan_id",
                  "install_date", "status", "metadata", "created_at", "updated_at")
         .insert(P(), P(), P(), P(), P(), P(), P(), P(),
                 ValueWrapper("active"), P(), P(), P()))
    conn.execute(q.get_sql(),
        (meter_id, meter_number, args.customer_id, service_type,
         args.name, args.address, args.rate_plan_id, args.install_date,
         metadata, now, now))
    conn.commit()
    audit(conn, "erpclaw-billing", "add-meter", "meter", meter_id,
           new_values={"meter_number": meter_number, "service_type": service_type})

    q = Q.from_(T_meter).select(T_meter.star).where(T_meter.id == P())
    meter = row_to_dict(conn.execute(q.get_sql(), (meter_id,)).fetchone())
    ok({"meter": meter})


def update_meter(conn, args):
    """Update meter configuration."""
    if not args.meter_id:
        err("--meter-id is required")

    q = Q.from_(T_meter).select(T_meter.star).where(T_meter.id == P())
    meter = conn.execute(q.get_sql(), (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    set_data, params, old_values = {}, [], {}

    if args.name is not None:
        old_values["service_point_id"] = meter["service_point_id"]
        set_data["service_point_id"] = P()
        params.append(args.name)

    if args.status is not None:
        if args.status not in VALID_METER_STATUSES:
            err(f"Invalid status: {args.status}. "
                 f"Must be one of: {', '.join(VALID_METER_STATUSES)}")
        old_values["status"] = meter["status"]
        set_data["status"] = P()
        params.append(args.status)

    if args.rate_plan_id is not None:
        rp_q = Q.from_(T_rate_plan).select(T_rate_plan.id).where(T_rate_plan.id == P())
        rp = conn.execute(rp_q.get_sql(), (args.rate_plan_id,)).fetchone()
        if not rp:
            err(f"Rate plan not found: {args.rate_plan_id}")
        old_values["rate_plan_id"] = meter["rate_plan_id"]
        set_data["rate_plan_id"] = P()
        params.append(args.rate_plan_id)

    if not set_data:
        err("No fields to update. Provide --name, --status, or --rate-plan-id")

    set_data["updated_at"] = P()
    params.append(_now())
    params.append(args.meter_id)

    q = Q.update(T_meter)
    for col, val in set_data.items():
        q = q.set(Field(col), val)
    q = q.where(T_meter.id == P())
    conn.execute(q.get_sql(), params)
    conn.commit()
    audit(conn, "erpclaw-billing", "update-meter", "meter", args.meter_id, old_values=old_values)

    q = Q.from_(T_meter).select(T_meter.star).where(T_meter.id == P())
    meter = row_to_dict(conn.execute(q.get_sql(), (args.meter_id,)).fetchone())
    ok({"meter": meter})


def get_meter(conn, args):
    """Get meter with latest reading."""
    if not args.meter_id:
        err("--meter-id is required")

    q = Q.from_(T_meter).select(T_meter.star).where(T_meter.id == P())
    meter = conn.execute(q.get_sql(), (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    result = row_to_dict(meter)

    q = (Q.from_(T_meter_reading).select(T_meter_reading.star)
         .where(T_meter_reading.meter_id == P())
         .orderby(T_meter_reading.reading_date, order=Order.desc)
         .limit(1))
    latest = conn.execute(q.get_sql(), (args.meter_id,)).fetchone()
    result["latest_reading"] = row_to_dict(latest) if latest else None

    q = (Q.from_(T_meter_reading)
         .select(fn.Count("*").as_("cnt"))
         .where(T_meter_reading.meter_id == P()))
    count = conn.execute(q.get_sql(), (args.meter_id,)).fetchone()
    result["reading_count"] = count["cnt"]
    ok({"meter": result})


def list_meters(conn, args):
    """List meters with optional filters."""
    m = Table("meter")
    c = Table("customer")
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)
    params = []

    # Build count query
    count_q = Q.from_(m).select(fn.Count("*").as_("cnt"))
    # Build data query
    data_q = (Q.from_(m).select(m.star, c.name.as_("customer_name"))
              .left_join(c).on(m.customer_id == c.id))

    if args.customer_id:
        count_q = count_q.where(m.customer_id == P())
        data_q = data_q.where(m.customer_id == P())
        params.append(args.customer_id)
    if args.meter_type:
        count_q = count_q.where(m.service_type == P())
        data_q = data_q.where(m.service_type == P())
        params.append(args.meter_type)
    if args.status:
        count_q = count_q.where(m.status == P())
        data_q = data_q.where(m.status == P())
        params.append(args.status)

    total_count = conn.execute(count_q.get_sql(), params).fetchone()["cnt"]

    data_q = data_q.orderby(m.created_at, order=Order.desc).limit(P()).offset(P())
    rows = conn.execute(data_q.get_sql(), params + [limit, offset]).fetchall()
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

    q = Q.from_(T_meter).select(T_meter.star).where(T_meter.id == P())
    meter = conn.execute(q.get_sql(), (args.meter_id,)).fetchone()
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

    reading_value = _check_number(args.reading_value, "--reading-value")
    previous_reading_value = None
    consumption = None

    if meter["last_reading_value"] is not None:
        # Gate the STORED previous value too (QA round-2 DEFECT-1): a legacy
        # non-finite last_reading_value would otherwise mint a fresh stored
        # 'NaN' consumption (NaN - x = NaN and `diff < 0` is False). Loud,
        # named data error; no reading row is written.
        try:
            previous_reading_value = _finite_decimal(meter["last_reading_value"])
        except (TypeError, ValueError, ArithmeticError):
            err(f"Stored last_reading_value for meter "
                 f"{meter['meter_number']} is not a valid number: "
                 f"{meter['last_reading_value']!r} (data error). Repair the "
                 f"meter record before adding readings.")
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
    now = _now()
    q = (Q.into(T_meter_reading)
         .columns("id", "meter_id", "reading_date", "reading_value",
                  "previous_reading_value", "consumption", "reading_type",
                  "uom", "source", "validated", "created_at")
         .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(),
                 ValueWrapper(0), P()))
    conn.execute(q.get_sql(),
        (reading_id, args.meter_id, args.reading_date,
         str(reading_value),
         str(previous_reading_value) if previous_reading_value is not None else None,
         str(consumption) if consumption is not None else None,
         reading_type, uom, source, now))

    q = (Q.update(T_meter)
         .set(T_meter.last_reading_date, P())
         .set(T_meter.last_reading_value, P())
         .set(T_meter.updated_at, P())
         .where(T_meter.id == P()))
    conn.execute(q.get_sql(),
        (args.reading_date, str(reading_value), now, args.meter_id))
    conn.commit()
    audit(conn, "erpclaw-billing", "add-meter-reading", "meter_reading", reading_id,
           new_values={"reading_value": str(reading_value),
                       "consumption": str(consumption) if consumption else None})

    q = Q.from_(T_meter_reading).select(T_meter_reading.star).where(T_meter_reading.id == P())
    reading = row_to_dict(conn.execute(q.get_sql(), (reading_id,)).fetchone())
    ok({"reading": reading})


def list_meter_readings(conn, args):
    """List meter readings with optional date filters."""
    if not args.meter_id:
        err("--meter-id is required")

    mr = Table("meter_reading")
    params = [args.meter_id]
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)

    count_q = Q.from_(mr).select(fn.Count("*").as_("cnt")).where(mr.meter_id == P())
    data_q = Q.from_(mr).select(mr.star).where(mr.meter_id == P())

    if args.from_date:
        count_q = count_q.where(mr.reading_date >= P())
        data_q = data_q.where(mr.reading_date >= P())
        params.append(args.from_date)
    if args.to_date:
        count_q = count_q.where(mr.reading_date <= P())
        data_q = data_q.where(mr.reading_date <= P())
        params.append(args.to_date)

    total_count = conn.execute(count_q.get_sql(), params).fetchone()["cnt"]

    data_q = data_q.orderby(mr.reading_date, order=Order.desc).limit(P()).offset(P())
    rows = conn.execute(data_q.get_sql(), params + [limit, offset]).fetchall()
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
    _check_number(args.quantity, "--quantity")

    q = Q.from_(T_meter).select(T_meter.id, T_meter.customer_id).where(T_meter.id == P())
    meter = conn.execute(q.get_sql(), (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    if args.idempotency_key:
        q = Q.from_(T_usage_event).select(T_usage_event.id).where(T_usage_event.idempotency_key == P())
        existing = conn.execute(q.get_sql(), (args.idempotency_key,)).fetchone()
        if existing:
            q = Q.from_(T_usage_event).select(T_usage_event.star).where(T_usage_event.id == P())
            evt = row_to_dict(conn.execute(q.get_sql(), (existing["id"],)).fetchone())
            ok({"usage_event": evt, "deduplicated": True})

    event_id = str(uuid.uuid4())
    event_type = args.event_type or "usage"

    q = (Q.into(T_usage_event)
         .columns("id", "customer_id", "meter_id", "event_type",
                  "quantity", "timestamp", "metadata", "idempotency_key",
                  "processed", "created_at")
         .insert(P(), P(), P(), P(), P(), P(), P(), P(),
                 ValueWrapper(0), P()))
    conn.execute(q.get_sql(),
        (event_id, meter["customer_id"], args.meter_id, event_type,
         args.quantity, args.event_date, args.properties, args.idempotency_key,
         _now()))
    conn.commit()
    audit(conn, "erpclaw-billing", "add-usage-event", "usage_event", event_id,
           new_values={"quantity": args.quantity, "event_type": event_type})

    q = Q.from_(T_usage_event).select(T_usage_event.star).where(T_usage_event.id == P())
    evt = row_to_dict(conn.execute(q.get_sql(), (event_id,)).fetchone())
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

        try:
            _finite_decimal(quantity)
        except _UnrepresentableAmount:
            errors.append({"index": i,
                           "error": (f"quantity must be a representable "
                                     f"amount, got: {quantity!r}")})
            continue
        except (TypeError, ValueError, ArithmeticError):
            errors.append({"index": i,
                           "error": f"quantity must be a number, got: {quantity!r}"})
            continue

        mq = Q.from_(T_meter).select(T_meter.id, T_meter.customer_id).where(T_meter.id == P())
        meter = conn.execute(mq.get_sql(), (meter_id,)).fetchone()
        if not meter:
            errors.append({"index": i, "error": f"Meter not found: {meter_id}"})
            continue

        if idempotency_key:
            iq = Q.from_(T_usage_event).select(T_usage_event.id).where(T_usage_event.idempotency_key == P())
            existing = conn.execute(iq.get_sql(), (idempotency_key,)).fetchone()
            if existing:
                duplicates += 1
                continue

        event_id = str(uuid.uuid4())
        ins_q = (Q.into(T_usage_event)
                 .columns("id", "customer_id", "meter_id", "event_type",
                          "quantity", "timestamp", "metadata", "idempotency_key",
                          "processed", "created_at")
                 .insert(P(), P(), P(), P(), P(), P(), P(), P(),
                         ValueWrapper(0), P()))
        conn.execute(ins_q.get_sql(),
            (event_id, meter["customer_id"], meter_id, event_type,
             str(quantity), event_date, metadata, idempotency_key, _now()))
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

def _insert_rate_tiers(conn, plan_id, tiers_data):
    """Insert tier rows for a plan (JSON-normalizing time_of_use_hours)."""
    for i, tier in enumerate(tiers_data):
        tier_id = str(uuid.uuid4())
        hours = tier.get("time_of_use_hours")
        if isinstance(hours, (list, dict)):
            hours = json.dumps(hours)
        tq = (Q.into(T_rate_tier)
              .columns("id", "rate_plan_id", "tier_start", "tier_end",
                       "rate", "fixed_charge", "time_of_use_period",
                       "time_of_use_hours", "demand_type", "sort_order")
              .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P()))
        conn.execute(tq.get_sql(),
            (tier_id, plan_id,
             tier.get("tier_start", "0"), tier.get("tier_end"),
             tier.get("rate", "0"), tier.get("fixed_charge"),
             tier.get("time_of_use_period"), hours,
             tier.get("demand_type"), i))


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

    # Parse + validate tiers/strategy BEFORE any insert (no partial writes).
    tiers_data = None
    if args.tiers:
        tiers_data = _parse_json_arg(args.tiers, "tiers")
        if not isinstance(tiers_data, list):
            err("--tiers must be a JSON array")

    tier_strategy_raw = getattr(args, "tier_strategy", None)
    metadata_json = None
    try:
        if plan_type == "hybrid":
            if not tier_strategy_raw:
                raise RatingError(
                    "hybrid plan requires --tier-strategy, e.g. "
                    '\'{"components": [{"type": "flat", '
                    '"tiers": [{"rate": "0"}], "base_charge": "100.00"}]}\'')
            strategy = _validate_tier_strategy(
                _parse_json_arg(tier_strategy_raw, "tier-strategy"))
            metadata_json = json.dumps({"tier_strategy": strategy})
        elif tier_strategy_raw:
            raise RatingError(
                "--tier-strategy is only valid for hybrid plans "
                f"(this plan is '{plan_type}')")
        # Numeric gate for ALL plan types (QA bounce D3: a flat tier rate
        # of "abc" must be rejected here, not stored and detonated at
        # rating time), plus plan-level money flags.
        _tier_numbers_checked(tiers_data or [], plan_type)
        for flag, value in (("--base-charge", args.base_charge),
                            ("--minimum-charge", args.minimum_charge),
                            ("--minimum-commitment", args.minimum_commitment),
                            ("--overage-rate", args.overage_rate)):
            if value is not None:
                _dec(value, flag)
        if plan_type == "time_of_use":
            _validate_tou_tiers(tiers_data or [])
        elif plan_type == "demand":
            _validate_demand_tiers(tiers_data or [])
    except RatingError as exc:
        err(str(exc))

    plan_id = str(uuid.uuid4())
    now = _now()
    q = (Q.into(T_rate_plan)
         .columns("id", "name", "service_type", "plan_type",
                  "base_charge", "base_charge_period", "currency", "effective_from",
                  "effective_to", "minimum_charge", "minimum_commitment", "overage_rate",
                  "metadata", "created_at", "updated_at")
         .insert(P(), P(), P(), P(), P(), P(), ValueWrapper("USD"), P(),
                 P(), P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(),
        (plan_id, args.name, args.service_type, plan_type,
         args.base_charge, args.base_charge_period,
         effective_from, args.effective_to,
         args.minimum_charge, args.minimum_commitment, args.overage_rate,
         metadata_json, now, now))

    if tiers_data:
        _insert_rate_tiers(conn, plan_id, tiers_data)

    conn.commit()
    audit(conn, "erpclaw-billing", "add-rate-plan", "rate_plan", plan_id,
           new_values={"name": args.name, "plan_type": plan_type})

    q = Q.from_(T_rate_plan).select(T_rate_plan.star).where(T_rate_plan.id == P())
    plan = row_to_dict(conn.execute(q.get_sql(), (plan_id,)).fetchone())
    q = (Q.from_(T_rate_tier).select(T_rate_tier.star)
         .where(T_rate_tier.rate_plan_id == P())
         .orderby(T_rate_tier.sort_order))
    tiers = [dict(r) for r in conn.execute(q.get_sql(), (plan_id,)).fetchall()]
    plan["tiers"] = tiers
    ok({"rate_plan": plan})


def update_rate_plan(conn, args):
    """Update rate plan configuration and/or tiers."""
    if not args.rate_plan_id:
        err("--rate-plan-id is required")

    q = Q.from_(T_rate_plan).select(T_rate_plan.star).where(T_rate_plan.id == P())
    plan = conn.execute(q.get_sql(), (args.rate_plan_id,)).fetchone()
    if not plan:
        err(f"Rate plan not found: {args.rate_plan_id}")

    set_data, params, old_values = {}, [], {}

    for field, col in [("name", "name"), ("base_charge", "base_charge"),
                       ("effective_to", "effective_to"),
                       ("minimum_charge", "minimum_charge"),
                       ("overage_rate", "overage_rate")]:
        val = getattr(args, field, None)
        if val is not None:
            if col in ("base_charge", "minimum_charge", "overage_rate"):
                try:
                    _dec(val, f"--{field.replace('_', '-')}")
                except RatingError as exc:
                    err(str(exc))
            old_values[col] = plan[col]
            set_data[col] = P()
            params.append(val)

    if set_data:
        set_data["updated_at"] = P()
        params.append(_now())
        params.append(args.rate_plan_id)
        uq = Q.update(T_rate_plan)
        for col, val in set_data.items():
            uq = uq.set(Field(col), val)
        uq = uq.where(T_rate_plan.id == P())
        conn.execute(uq.get_sql(), params)

    if args.tiers:
        tiers_data = _parse_json_arg(args.tiers, "tiers")
        if not isinstance(tiers_data, list):
            err("--tiers must be a JSON array")
        try:
            _tier_numbers_checked(tiers_data, plan["plan_type"])
            if plan["plan_type"] == "time_of_use":
                _validate_tou_tiers(tiers_data)
            elif plan["plan_type"] == "demand":
                _validate_demand_tiers(tiers_data)
        except RatingError as exc:
            err(str(exc))
        dq = Q.from_(T_rate_tier).delete().where(T_rate_tier.rate_plan_id == P())
        conn.execute(dq.get_sql(), (args.rate_plan_id,))
        _insert_rate_tiers(conn, args.rate_plan_id, tiers_data)

    tier_strategy_raw = getattr(args, "tier_strategy", None)
    if tier_strategy_raw:
        try:
            if plan["plan_type"] != "hybrid":
                raise RatingError(
                    "--tier-strategy is only valid for hybrid plans "
                    f"(this plan is '{plan['plan_type']}')")
            strategy = _validate_tier_strategy(
                _parse_json_arg(tier_strategy_raw, "tier-strategy"))
        except RatingError as exc:
            err(str(exc))
        try:
            existing_meta = json.loads(plan["metadata"]) if plan["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            existing_meta = {}
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        existing_meta["tier_strategy"] = strategy
        conn.execute(
            "UPDATE rate_plan SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(existing_meta), _now(), args.rate_plan_id))

    if not set_data and not args.tiers and not tier_strategy_raw:
        err("No fields to update")

    conn.commit()
    audit(conn, "erpclaw-billing", "update-rate-plan", "rate_plan", args.rate_plan_id,
           old_values=old_values)

    q = Q.from_(T_rate_plan).select(T_rate_plan.star).where(T_rate_plan.id == P())
    plan = row_to_dict(conn.execute(q.get_sql(), (args.rate_plan_id,)).fetchone())
    q = (Q.from_(T_rate_tier).select(T_rate_tier.star)
         .where(T_rate_tier.rate_plan_id == P())
         .orderby(T_rate_tier.sort_order))
    tiers = [dict(r) for r in conn.execute(q.get_sql(), (args.rate_plan_id,)).fetchall()]
    plan["tiers"] = tiers
    ok({"rate_plan": plan})


def get_rate_plan(conn, args):
    """Get rate plan with tiers."""
    if not args.rate_plan_id:
        err("--rate-plan-id is required")

    q = Q.from_(T_rate_plan).select(T_rate_plan.star).where(T_rate_plan.id == P())
    plan = conn.execute(q.get_sql(), (args.rate_plan_id,)).fetchone()
    if not plan:
        err(f"Rate plan not found: {args.rate_plan_id}")

    result = row_to_dict(plan)
    q = (Q.from_(T_rate_tier).select(T_rate_tier.star)
         .where(T_rate_tier.rate_plan_id == P())
         .orderby(T_rate_tier.sort_order))
    tiers = [dict(r) for r in conn.execute(q.get_sql(), (args.rate_plan_id,)).fetchall()]
    result["tiers"] = tiers
    ok({"rate_plan": result})


def list_rate_plans(conn, args):
    """List rate plans with optional filters."""
    params = []
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)

    count_q = Q.from_(T_rate_plan).select(fn.Count("*").as_("cnt"))
    data_q = Q.from_(T_rate_plan).select(T_rate_plan.star)

    if args.service_type:
        count_q = count_q.where(T_rate_plan.service_type == P())
        data_q = data_q.where(T_rate_plan.service_type == P())
        params.append(args.service_type)

    total_count = conn.execute(count_q.get_sql(), params).fetchone()["cnt"]

    data_q = data_q.orderby(T_rate_plan.created_at, order=Order.desc).limit(P()).offset(P())
    rows = conn.execute(data_q.get_sql(), params + [limit, offset]).fetchall()
    ok({"rate_plans": [dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# Internal: rate calculation engine
# ---------------------------------------------------------------------------

class RatingError(Exception):
    """Rating-engine data/configuration error.

    The engine never calls err() (which would sys.exit and abort a whole
    bill run for one bad meter). Single-target actions (rate-consumption,
    add/update-rate-plan) catch this and err(); run-billing catches it per
    meter and collects a loud error entry in the run output (S1.2a: a data
    error is reported, never silently skipped).
    """


def _dec(value, context):
    """Finite to_decimal that fails as a RatingError naming the field.

    to_decimal raises ValueError/TypeError on garbage, which the action
    wrappers do NOT catch — the user would see "An unexpected error
    occurred" (QA bounce D1) and run_billing would abort wholesale (D3).
    Non-finite values ('NaN'/'Infinity') parse but poison money math (QA
    round-2 DEFECT-1), and finite-but-unrepresentable magnitudes pass
    is_finite() but detonate round_currency (QA round-3) — both are
    rejected here too, as the same named RatingError class.
    Every money/quantity conversion inside the rating engine routes through
    here so bad data fails as a caught, named validation error instead.
    """
    try:
        return _finite_decimal(value)
    except _UnrepresentableAmount:
        raise RatingError(
            f"{context}: {value!r} is not a representable amount "
            f"(magnitude exceeds supported currency precision)")
    except (TypeError, ValueError, ArithmeticError):
        raise RatingError(f"{context}: {value!r} is not a valid number")


def _tier_numbers_checked(tiers, label):
    """Validate every numeric tier field parses as Decimal (RatingError if not).

    Used both at write time (add/update-rate-plan, so garbage never enters
    the DB) and at rating time (so legacy garbage rows fail as a contained
    RatingError, never an uncaught ValueError). Returns the tiers unchanged.
    """
    for i, tier in enumerate(tiers or []):
        if not isinstance(tier, dict):
            raise RatingError(f"{label} tier {i + 1} must be a JSON object")
        for field_name in ("tier_start", "tier_end", "rate", "fixed_charge"):
            if tier.get(field_name) is not None:
                _dec(tier[field_name], f"{label} tier {i + 1} {field_name}")
    return tiers


def _parse_hhmm(value, context):
    """'HH:MM' -> minutes since midnight. '24:00' allowed as range end."""
    parts = str(value).split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise RatingError(f"Invalid time '{value}' in {context}: expected HH:MM")
    hours, minutes = int(parts[0]), int(parts[1])
    if minutes > 59 or hours > 24 or (hours == 24 and minutes != 0):
        raise RatingError(f"Invalid time '{value}' in {context}: expected HH:MM")
    return hours * 60 + minutes


def _tou_ranges_for_tier(tier):
    """Parse a TOU tier's time_of_use_hours into [(start_min, end_min)].

    Accepts a JSON list of "HH:MM-HH:MM" ranges, or the wave-file dict shape
    {"peak": [...], "off_peak": [...], ...} from which the tier's own period
    key is taken. Cross-midnight ranges must be split (e.g. off_peak =
    ["00:00-06:00", "22:00-24:00"]).
    """
    period = tier.get("time_of_use_period")
    raw = tier.get("time_of_use_hours")
    if not raw:
        raise RatingError(
            f"TOU tier for period '{period}' is missing time_of_use_hours")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise RatingError(
                f"time_of_use_hours for period '{period}' is not valid JSON")
    if isinstance(raw, dict):
        raw = raw.get(period)
        if raw is None:
            raise RatingError(
                f"time_of_use_hours dict has no entry for period '{period}'")
    if not isinstance(raw, list) or not raw:
        raise RatingError(
            f"time_of_use_hours for period '{period}' must be a non-empty "
            f"list of 'HH:MM-HH:MM' ranges")
    ranges = []
    for item in raw:
        pieces = str(item).split("-")
        if len(pieces) != 2:
            raise RatingError(
                f"Invalid TOU range '{item}' for period '{period}': "
                f"expected 'HH:MM-HH:MM'")
        start = _parse_hhmm(pieces[0], f"TOU period '{period}'")
        end = _parse_hhmm(pieces[1], f"TOU period '{period}'")
        if end <= start:
            raise RatingError(
                f"Invalid TOU range '{item}' for period '{period}': end must "
                f"be after start (split cross-midnight ranges, e.g. "
                f"'22:00-24:00' + '00:00-06:00')")
        ranges.append((start, end))
    return ranges


def _validate_tou_tiers(tiers):
    """Validate a time_of_use plan's tiers: 24h coverage, no overlap.

    Rules: at least one tier; every tier names a valid period; each period
    appears in at most one tier (period<->rate is 1:1); the union of all
    ranges covers exactly 00:00-24:00 with no gaps and no overlaps.
    Returns {period: {"ranges": [...], "rate": Decimal}}.
    """
    if not tiers:
        raise RatingError("time_of_use plan requires at least one tier")
    seen_periods = {}
    all_ranges = []
    for tier in tiers:
        period = tier.get("time_of_use_period")
        if period not in VALID_TOU_PERIODS:
            raise RatingError(
                f"TOU tier has invalid time_of_use_period '{period}'. "
                f"Must be one of: {', '.join(VALID_TOU_PERIODS)}")
        if period in seen_periods:
            raise RatingError(
                f"TOU period '{period}' appears in more than one tier; "
                f"each period must map to exactly one rate")
        ranges = _tou_ranges_for_tier(tier)
        seen_periods[period] = {
            "ranges": ranges,
            "rate": _dec(tier.get("rate", "0"),
                         f"TOU tier for period '{period}' rate"),
        }
        all_ranges.extend((start, end, period) for start, end in ranges)

    all_ranges.sort()
    cursor = 0
    for start, end, period in all_ranges:
        if start < cursor:
            raise RatingError(
                f"TOU hours overlap at {cursor // 60:02d}:{cursor % 60:02d} "
                f"(period '{period}'); ranges must not overlap")
        if start > cursor:
            raise RatingError(
                f"TOU hours leave a gap from {cursor // 60:02d}:"
                f"{cursor % 60:02d} to {start // 60:02d}:{start % 60:02d}; "
                f"the plan must cover all 24 hours")
        cursor = end
    if cursor != MINUTES_PER_DAY:
        raise RatingError(
            f"TOU hours end at {cursor // 60:02d}:{cursor % 60:02d}; "
            f"the plan must cover all 24 hours (up to 24:00)")
    return seen_periods


def _time_of_day_minutes(timestamp, context):
    """Extract minutes-since-midnight from a timestamp string.

    Returns None when the value is date-only (no time component) — the
    caller decides whether that is an error (TOU rating: it is, loudly).
    """
    text = str(timestamp).strip().replace("T", " ")
    if " " not in text:
        return None
    time_part = text.split(" ", 1)[1]
    pieces = time_part.split(":")
    if len(pieces) < 2:
        raise RatingError(f"Unparseable timestamp '{timestamp}' in {context}")
    try:
        hours, minutes = int(pieces[0]), int(pieces[1])
    except ValueError:
        raise RatingError(f"Unparseable timestamp '{timestamp}' in {context}")
    if hours > 23 or minutes > 59:
        raise RatingError(f"Unparseable timestamp '{timestamp}' in {context}")
    return hours * 60 + minutes


def _partition_tou_usage(usage_events, period_map):
    """Partition usage rows into {period: Decimal qty} via the TOU ranges.

    usage_events: iterable of {"timestamp": ..., "quantity": ...,
    "source": ...}. A row with a date-only timestamp and nonzero quantity is
    a loud error — attributing it to a period would be a silent guess.
    """
    usage_by_period = {}
    for row in usage_events:
        qty = _dec(row.get("quantity") or "0",
                   f"{row.get('source', 'usage row')} quantity")
        if qty == 0:
            continue
        minutes = _time_of_day_minutes(
            row.get("timestamp"), row.get("source", "usage row"))
        if minutes is None:
            raise RatingError(
                f"time_of_use plan requires time-stamped usage; "
                f"{row.get('source', 'usage row')} has date-only timestamp "
                f"'{row.get('timestamp')}' and cannot be assigned to a "
                f"peak/off_peak/shoulder period")
        matched = None
        for period, info in period_map.items():
            if any(start <= minutes < end for start, end in info["ranges"]):
                matched = period
                break
        if matched is None:
            # Unreachable when the validator passed (full 24h coverage);
            # defensive against pre-validator legacy tier rows.
            raise RatingError(
                f"No TOU period covers time "
                f"{minutes // 60:02d}:{minutes % 60:02d}; the plan's "
                f"time_of_use_hours do not cover all 24 hours")
        usage_by_period[matched] = usage_by_period.get(
            matched, Decimal("0")) + qty
    return usage_by_period


def _validate_demand_tiers(tiers):
    """A demand plan needs exactly one 'demand' tier; 'energy' is optional."""
    demand_tiers = [t for t in (tiers or [])
                    if t.get("demand_type") == "demand"]
    if not demand_tiers:
        raise RatingError(
            "demand plan requires a tier with demand_type='demand' "
            "(the per-kW demand rate)")
    if len(demand_tiers) > 1:
        raise RatingError(
            "demand plan must have exactly one tier with "
            "demand_type='demand'")
    energy_tiers = [t for t in tiers if t.get("demand_type") == "energy"]
    if len(energy_tiers) > 1:
        raise RatingError(
            "demand plan must have at most one tier with "
            "demand_type='energy'")
    return demand_tiers[0], (energy_tiers[0] if energy_tiers else None)


def _validate_tier_strategy(strategy):
    """Validate a hybrid plan's tier_strategy JSON shape.

    Shape: {"components": [{"type": <one of HYBRID_COMPONENT_TYPES>,
    "tiers": [...], "base_charge"?: str, "minimum_charge"?: str}, ...]}.
    No nested hybrid; prepaid_credit is not composable (see
    HYBRID_COMPONENT_TYPES comment).
    """
    if not isinstance(strategy, dict):
        raise RatingError("tier-strategy must be a JSON object")
    components = strategy.get("components")
    if not isinstance(components, list) or not components:
        raise RatingError(
            "tier-strategy must have a non-empty 'components' array")
    for i, comp in enumerate(components):
        if not isinstance(comp, dict):
            raise RatingError(f"tier-strategy component {i} must be an object")
        ctype = comp.get("type")
        if ctype == "hybrid":
            raise RatingError(
                "tier-strategy components must not be 'hybrid' "
                "(no nested hybrid plans)")
        if ctype == "prepaid_credit":
            raise RatingError(
                "tier-strategy components must not be 'prepaid_credit': "
                "prepaid credit is a settlement mechanism, not a rate shape; "
                "composing it would deduct the balance twice")
        if ctype not in HYBRID_COMPONENT_TYPES:
            raise RatingError(
                f"tier-strategy component {i} has invalid type '{ctype}'. "
                f"Must be one of: {', '.join(HYBRID_COMPONENT_TYPES)}")
        comp_tiers = comp.get("tiers")
        if not isinstance(comp_tiers, list) or not comp_tiers:
            raise RatingError(
                f"tier-strategy component {i} ('{ctype}') must declare a "
                f"non-empty 'tiers' array")
        _tier_numbers_checked(
            comp_tiers, f"tier-strategy component {i} ('{ctype}')")
        for money_field in ("base_charge", "minimum_charge"):
            if comp.get(money_field) is not None:
                # RatingError on garbage (caught validation error, D1)
                _dec(comp[money_field],
                     f"tier-strategy component {i} {money_field}")
        if ctype == "time_of_use":
            _validate_tou_tiers(comp_tiers)
        elif ctype == "demand":
            _validate_demand_tiers(comp_tiers)
    return strategy


def _calculate_charge(plan_type, tiers, consumption, base_charge="0",
                      minimum_charge=None, usage_by_period=None,
                      usage_events=None, peak_demand=None,
                      tier_strategy=None):
    """Pure function: calculate charges for a given consumption amount.

    All 7 plan types (Wave F S1.1). Extra context per type:
    - time_of_use: usage_by_period ({period: qty}) or usage_events
      (list of {"timestamp","quantity","source"}) — partitioned here.
    - demand: peak_demand (billing_period.peak_demand / --peak-demand).
    - hybrid: tier_strategy (validated dict; components recurse).
    - prepaid_credit: charge math only — the credit deduction is applied by
      the caller (run-billing deducts inside its transaction;
      rate-consumption previews without deducting).
    Raises RatingError on any data/configuration error. Exact Decimal math,
    ROUND_HALF_UP, TEXT in/out.
    """
    consumption = _dec(consumption, "consumption")
    base = _dec(base_charge or "0", "base_charge")
    breakdown = []
    demand_component = None

    if plan_type == "flat":
        if not tiers:
            raise RatingError("Flat rate plan requires at least one tier")
        _tier_numbers_checked(tiers, plan_type)
        rate = to_decimal(tiers[0].get("rate", "0"))
        usage_charge = round_currency(consumption * rate)
        breakdown.append({
            "tier": "flat", "consumption": str(consumption),
            "rate": str(rate), "charge": str(usage_charge),
        })

    elif plan_type in ("tiered", "prepaid_credit"):
        # prepaid_credit uses tiered charge math (a single open-ended tier
        # behaves as flat); the credit-balance deduction happens in the
        # caller against prepaid_credit_balance.
        if not tiers:
            raise RatingError(
                f"{plan_type} plan requires at least one tier")
        _tier_numbers_checked(tiers, plan_type)
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
        _tier_numbers_checked(tiers, plan_type)
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

    elif plan_type == "time_of_use":
        period_map = _validate_tou_tiers(tiers)
        if usage_by_period is None:
            if usage_events is None:
                raise RatingError(
                    "time_of_use rating needs partitioned usage: pass "
                    "usage-by-period (e.g. '{\"peak\": \"120\"}') or "
                    "time-stamped usage rows")
            usage_by_period = _partition_tou_usage(usage_events, period_map)
        usage_charge = Decimal("0")
        partitioned_total = Decimal("0")
        for period, qty in usage_by_period.items():
            if period not in period_map:
                raise RatingError(
                    f"usage-by-period names unknown TOU period '{period}'. "
                    f"Plan periods: {', '.join(sorted(period_map))}")
            qty = _dec(qty, f"usage-by-period quantity for '{period}'")
            if qty < 0:
                raise RatingError(
                    f"usage-by-period quantity for '{period}' is negative")
            rate = period_map[period]["rate"]
            charge = round_currency(qty * rate)
            usage_charge += charge
            partitioned_total += qty
            breakdown.append({
                "tier": "time_of_use", "period": period,
                "consumption": str(qty), "rate": str(rate),
                "charge": str(charge),
            })
        consumption = partitioned_total

    elif plan_type == "demand":
        demand_tier, energy_tier = _validate_demand_tiers(tiers)
        _tier_numbers_checked(tiers, plan_type)
        if peak_demand is None:
            raise RatingError(
                "demand rating requires peak demand (billing_period."
                "peak_demand, or --peak-demand on rate-consumption)")
        peak = _dec(peak_demand, "peak demand")
        if peak < 0:
            raise RatingError("peak demand must not be negative")
        demand_rate = to_decimal(demand_tier.get("rate", "0"))
        demand_component = round_currency(peak * demand_rate)
        # usage_charge carries BOTH components so that downstream totals
        # (subtotal = base + usage + adjustments, recomputed by
        # add-billing-adjustment) stay correct for every plan type;
        # demand_charge is reported (and stored) as the demand subset.
        usage_charge = demand_component
        breakdown.append({
            "tier": "demand", "demand_type": "demand",
            "peak_demand": str(peak), "rate": str(demand_rate),
            "charge": str(demand_component),
        })
        if energy_tier is not None:
            energy_rate = to_decimal(energy_tier.get("rate", "0"))
            energy_charge = round_currency(consumption * energy_rate)
            usage_charge += energy_charge
            breakdown.append({
                "tier": "demand", "demand_type": "energy",
                "consumption": str(consumption), "rate": str(energy_rate),
                "charge": str(energy_charge),
            })

    elif plan_type == "hybrid":
        strategy = _validate_tier_strategy(tier_strategy)
        usage_charge = Decimal("0")
        for i, comp in enumerate(strategy["components"]):
            comp_result = _calculate_charge(
                comp["type"], comp["tiers"], str(consumption),
                base_charge=comp.get("base_charge", "0"),
                minimum_charge=comp.get("minimum_charge"),
                usage_by_period=usage_by_period,
                usage_events=usage_events,
                peak_demand=peak_demand,
            )
            comp_total = to_decimal(comp_result["total_charge"])
            usage_charge += comp_total
            breakdown.append({
                "tier": "hybrid_component", "component_index": i,
                "component_type": comp["type"],
                "charge": str(comp_total),
                "breakdown": comp_result["breakdown"],
            })

    else:
        raise RatingError(
            f"Unknown plan_type '{plan_type}' — this is a data error. "
            f"Valid plan types: {', '.join(VALID_PLAN_TYPES)}")

    total = round_currency(base + usage_charge)
    if minimum_charge:
        min_charge = _dec(minimum_charge, "minimum_charge")
        if total < min_charge:
            total = min_charge

    result = {
        "usage_charge": str(usage_charge),
        "base_charge": str(base),
        "total_charge": str(total),
        "breakdown": breakdown,
    }
    if demand_component is not None:
        result["demand_charge"] = str(demand_component)
    return result


def _apply_prepaid_deduction(conn, customer_id, charge, as_of_date=None,
                             preview=False):
    """Deduct a rated charge from the customer's prepaid_credit_balance rows.

    Consumes active, unexpired balances ordered by period_end then
    created_at (use-before-lose). Insufficient aggregate balance is an
    explicit over_limit outcome with NO deduction (never a silent partial
    burn); the caller decides what to do with the still-owed charge.
    preview=True computes the outcome without writing (rate-consumption).
    Writes ride the caller's transaction — no commit here.
    """
    if not customer_id:
        raise RatingError(
            "prepaid_credit rating requires a customer (to locate the "
            "prepaid balance)")
    charge = round_currency(_dec(charge, "prepaid charge"))
    as_of = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows = conn.execute(
        """SELECT id, remaining_amount, period_end FROM prepaid_credit_balance
           WHERE customer_id = ? AND status = 'active'
           ORDER BY period_end ASC, created_at ASC""",
        (customer_id,)).fetchall()

    usable = []
    available = Decimal("0")
    for row in rows:
        if row["period_end"] and str(row["period_end"]) < as_of:
            continue  # expired but not yet marked; never consume
        remaining = _dec(row["remaining_amount"] or "0",
                         f"prepaid credit {row['id']} remaining_amount")
        if remaining <= 0:
            continue
        usable.append((row["id"], remaining))
        available += remaining

    outcome = {
        "charge": str(charge),
        "available_credit": str(round_currency(available)),
        "over_limit": False,
        "deducted": "0.00",
        "remaining_credit": str(round_currency(available)),
    }
    if preview:
        outcome["preview"] = True

    if charge > available:
        outcome["over_limit"] = True
        outcome["credit_shortfall"] = str(round_currency(charge - available))
        return outcome

    if charge > 0 and not preview:
        now = _now()
        needed = charge
        for credit_id, remaining in usable:
            if needed <= 0:
                break
            take = min(remaining, needed)
            new_remaining = round_currency(remaining - take)
            new_status = "exhausted" if new_remaining <= 0 else "active"
            conn.execute(
                """UPDATE prepaid_credit_balance
                   SET remaining_amount = ?, status = ?, updated_at = ?
                   WHERE id = ?""",
                (str(new_remaining), new_status, now, credit_id))
            needed -= take
        # Only a REAL deduction reports as deducted (QA bounce D2: a
        # preview must never claim money moved that didn't).
        outcome["deducted"] = str(charge)
        outcome["remaining_credit"] = str(round_currency(available - charge))
    elif preview:
        # Truthful preview: deducted stays 0.00 and remaining_credit stays
        # the CURRENT balance; the projection lives in its own keys.
        outcome["would_deduct"] = str(charge)
        outcome["projected_remaining_credit"] = str(
            round_currency(available - charge))
    return outcome


def _load_tier_strategy(plan):
    """Read a hybrid plan's tier_strategy from rate_plan.metadata JSON."""
    metadata = plan["metadata"]
    if metadata:
        try:
            parsed = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            raise RatingError(
                "hybrid plan metadata is not valid JSON (data error)")
        strategy = parsed.get("tier_strategy") if isinstance(parsed, dict) else None
        if strategy:
            return strategy
    raise RatingError(
        "hybrid plan has no tier_strategy. Recreate or update the plan "
        "with --tier-strategy "
        "'{\"components\": [{\"type\": \"flat\", \"tiers\": [...]}]}'")


def rate_consumption(conn, args):
    """Pure function: calculate charges for consumption against a rate plan.

    Never mutates state — a prepaid_credit plan gets a balance PREVIEW here
    (the actual deduction happens in run-billing, the action that rates
    billing periods; deducting in a preview would double-deduct).
    """
    if not args.rate_plan_id:
        err("--rate-plan-id is required")
    if not args.consumption:
        err("--consumption is required")

    q = Q.from_(T_rate_plan).select(T_rate_plan.star).where(T_rate_plan.id == P())
    plan = conn.execute(q.get_sql(), (args.rate_plan_id,)).fetchone()
    if not plan:
        err(f"Rate plan not found: {args.rate_plan_id}")

    plan_type = plan["plan_type"]
    if plan_type not in VALID_PLAN_TYPES:
        err(f"Rate plan has unknown plan_type '{plan_type}' — this is a "
             f"data error. Valid: {', '.join(VALID_PLAN_TYPES)}")

    q = (Q.from_(T_rate_tier).select(T_rate_tier.star)
         .where(T_rate_tier.rate_plan_id == P())
         .orderby(T_rate_tier.sort_order))
    tiers = [dict(r) for r in conn.execute(q.get_sql(), (args.rate_plan_id,)).fetchall()]

    usage_by_period = None
    raw_periods = getattr(args, "usage_by_period", None)
    if raw_periods:
        usage_by_period = _parse_json_arg(raw_periods, "usage-by-period")
        if not isinstance(usage_by_period, dict):
            err("--usage-by-period must be a JSON object like "
                 '{"peak": "120", "off_peak": "300"}')

    try:
        if plan_type == "time_of_use" and usage_by_period is None:
            raise RatingError(
                "time_of_use rating needs --usage-by-period, e.g. "
                '\'{"peak": "120", "off_peak": "300"}\'')
        tier_strategy = None
        if plan_type == "hybrid":
            tier_strategy = _load_tier_strategy(plan)
        result = _calculate_charge(
            plan_type, tiers, args.consumption,
            base_charge=plan["base_charge"],
            minimum_charge=plan["minimum_charge"],
            usage_by_period=usage_by_period,
            peak_demand=getattr(args, "peak_demand", None),
            tier_strategy=tier_strategy,
        )
        if plan_type == "prepaid_credit":
            if not getattr(args, "customer_id", None):
                raise RatingError(
                    "prepaid_credit rating requires --customer-id to "
                    "preview the prepaid balance")
            result["prepaid"] = _apply_prepaid_deduction(
                conn, args.customer_id, result["total_charge"],
                preview=True)
    except RatingError as exc:
        err(str(exc))
    except ArithmeticError as exc:
        # Round-1 D1 contract: a named error, never the generic message.
        # Single bad inputs/rows are RatingErrors above; this leg names the
        # arithmetic residue (e.g. a product of individually-representable
        # amounts overflowing currency precision).
        err(f"rating produced an unrepresentable amount "
             f"({exc.__class__.__name__}): a computed charge exceeds the "
             f"supported currency magnitude (data error)")

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

    q = Q.from_(T_customer).select(T_customer.id).where(T_customer.id == P())
    cust = conn.execute(q.get_sql(), (args.customer_id,)).fetchone()
    if not cust:
        err(f"Customer not found: {args.customer_id}")

    q = Q.from_(T_meter).select(T_meter.star).where(T_meter.id == P())
    meter = conn.execute(q.get_sql(), (args.meter_id,)).fetchone()
    if not meter:
        err(f"Meter not found: {args.meter_id}")

    rate_plan_id = args.rate_plan_id or meter["rate_plan_id"]
    if not rate_plan_id:
        err("No rate plan specified. Provide --rate-plan-id or assign one to the meter")

    q = Q.from_(T_rate_plan).select(T_rate_plan.id).where(T_rate_plan.id == P())
    rp = conn.execute(q.get_sql(), (rate_plan_id,)).fetchone()
    if not rp:
        err(f"Rate plan not found: {rate_plan_id}")

    # Check for overlapping period
    q = (Q.from_(T_billing_period).select(T_billing_period.id)
         .where(T_billing_period.meter_id == P())
         .where(T_billing_period.status != ValueWrapper("void"))
         .where(T_billing_period.period_start <= P())
         .where(T_billing_period.period_end >= P()))
    overlap = conn.execute(q.get_sql(),
        (args.meter_id, args.to_date, args.from_date)).fetchone()
    if overlap:
        err(f"Overlapping billing period exists: {overlap['id']}")

    period_id = str(uuid.uuid4())
    now = _now()
    q = (Q.into(T_billing_period)
         .columns("id", "customer_id", "meter_id", "rate_plan_id",
                  "period_start", "period_end", "total_consumption", "base_charge",
                  "usage_charge", "adjustments_total", "subtotal", "tax_amount",
                  "grand_total", "status", "created_at", "updated_at")
         .insert(P(), P(), P(), P(), P(), P(),
                 ValueWrapper("0"), ValueWrapper("0"), ValueWrapper("0"),
                 ValueWrapper("0"), ValueWrapper("0"), ValueWrapper("0"),
                 ValueWrapper("0"), ValueWrapper("open"), P(), P()))
    conn.execute(q.get_sql(),
        (period_id, args.customer_id, args.meter_id, rate_plan_id,
         args.from_date, args.to_date, now, now))
    conn.commit()
    audit(conn, "erpclaw-billing", "create-billing-period", "billing_period", period_id,
           new_values={"period_start": args.from_date, "period_end": args.to_date})

    q = Q.from_(T_billing_period).select(T_billing_period.star).where(T_billing_period.id == P())
    bp = row_to_dict(conn.execute(q.get_sql(), (period_id,)).fetchone())
    ok({"billing_period": bp})


def run_billing(conn, args):
    """Execute bill run: aggregate usage, rate consumption, create periods."""
    if not args.company_id:
        err("--company-id is required")
    if not args.billing_date:
        err("--billing-date is required")

    q = Q.from_(T_company).select(T_company.id).where(T_company.id == P())
    company = conn.execute(q.get_sql(), (args.company_id,)).fetchone()
    if not company:
        err(f"Company not found: {args.company_id}")

    billing_date = args.billing_date
    from_date = args.from_date
    to_date = args.to_date or billing_date

    if not from_date:
        bd = datetime.strptime(billing_date, "%Y-%m-%d")
        from_date = (bd - timedelta(days=30)).strftime("%Y-%m-%d")

    # Find all customers for this company
    q = Q.from_(T_customer).select(T_customer.id).where(T_customer.company_id == P())
    customers = conn.execute(q.get_sql(), (args.company_id,)).fetchall()
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

    run_id = billing_run_lib.start(
        conn, "usage_billing", billing_date,
        [("meter", m["id"]) for m in meters],
        company_id=args.company_id,
        params={"from_date": from_date, "to_date": to_date,
                "billing_date": billing_date},
    )
    state = _run_billing_targets(conn, run_id, from_date, to_date, billing_date)
    summary = billing_run_lib.finalize(conn, run_id)

    audit(conn, "erpclaw-billing", "run-billing", "billing_period",
          ",".join(state["created_periods"]),
          new_values={"periods": len(state["created_periods"]),
                      "errors": len(state["errors"]),
                      "billing_run_id": run_id,
                      "total_billed": str(round_currency(state["total_billed"]))})
    conn.commit()

    result = {
        "periods_created": len(state["created_periods"]),
        "period_ids": state["created_periods"],
        "total_billed": str(round_currency(state["total_billed"])),
        "already_billed": state["already_billed"],
        "error_count": len(state["errors"]),
        "errors": state["errors"],
        "billing_run_id": run_id,
        "run_status": summary["status"],
    }
    if state["prepaid_outcomes"]:
        result["prepaid"] = state["prepaid_outcomes"]
    ok(result)


def _run_billing_targets(conn, run_id, from_date, to_date, billing_date):
    """Process every resumable target of a usage_billing run (S1.3).

    One transaction per meter via billing_run.process_target: the billing
    period write, usage-event marking and prepaid deduction commit atomically
    with the target's 'done' status — a crash rolls the whole meter back and
    resume re-runs it without double-billing (the period upsert is naturally
    keyed on meter+window). Returns the accumulated legacy output state.
    """
    state = {
        "created_periods": [],
        "total_billed": Decimal("0"),
        # S1.2a: a meter that cannot be rated is a REPORTED per-meter error
        # in the run output — never a silent skip. Only "already billed" is
        # a legitimate quiet skip (idempotency, counted below).
        "errors": [],
        "already_billed": 0,
        "prepaid_outcomes": [],
    }

    def _callback(cb_conn, target):
        return _bill_one_meter(cb_conn, target["target_id"], from_date,
                               to_date, billing_date)

    for target in billing_run_lib.list_targets(
            conn, run_id, statuses=billing_run_lib.RESUMABLE_TARGET_STATUSES):
        outcome = billing_run_lib.process_target(conn, run_id, target, _callback)
        if outcome["status"] == "done":
            res = outcome["result"]
            state["created_periods"].append(res["voucher_id"])
            state["total_billed"] += to_decimal(res["total_charge"])
            if res.get("prepaid_outcome") is not None:
                state["prepaid_outcomes"].append(res["prepaid_outcome"])
        elif outcome["status"] == "skipped":
            state["already_billed"] += 1
        elif outcome["status"] == "failed":
            info = conn.execute(
                "SELECT meter_number, customer_id FROM meter WHERE id = ?",
                (target["target_id"],)).fetchone()
            state["errors"].append({
                "meter_id": target["target_id"],
                "meter_number": info["meter_number"] if info else None,
                "customer_id": info["customer_id"] if info else None,
                "error": outcome["error"],
            })
        # "already_done": idempotent no-op (fresh runs never hit it)
    return state


def _bill_one_meter(conn, meter_id, from_date, to_date, billing_date):
    """Rate + bill ONE meter inside the caller's per-target transaction.

    Raises billing_run.SkipTarget when the window is already billed
    (natural-key idempotency), RatingError for any data error (stage-1
    loud-error contract — the library records it as a failed target and the
    run continues). Returns the target result for the run output.
    """
    meter = conn.execute(
        "SELECT * FROM meter WHERE id = ?", (meter_id,)).fetchone()
    if not meter:
        raise RatingError(f"Meter not found: {meter_id} (data error — the "
                          f"run references a meter that no longer exists)")
    rate_plan_id = meter["rate_plan_id"]
    if meter["status"] != "active" or not rate_plan_id:
        # Eligible at run start, changed since (resume path): benign skip,
        # never a guess-bill against a detached/inactive meter.
        raise billing_run_lib.SkipTarget(
            f"Meter {meter['meter_number']} is no longer eligible "
            f"(status '{meter['status']}', rate_plan_id "
            f"{rate_plan_id or 'NULL'})")

    # Skip if already billed for this period (idempotent re-run)
    existing = conn.execute(
        """SELECT id, status, peak_demand FROM billing_period
           WHERE meter_id = ? AND period_start = ? AND period_end = ?
           AND status NOT IN ('void')""",
        (meter_id, from_date, to_date)).fetchone()
    if existing and existing["status"] in ("rated", "invoiced", "paid"):
        raise billing_run_lib.SkipTarget(
            f"Already billed for {from_date}..{to_date} "
            f"(billing_period {existing['id']}, status {existing['status']})")

    # Load rate plan + tiers — a missing plan row or unknown plan_type
    # is a data error, reported loud (was: silent `continue`)
    pq = Q.from_(T_rate_plan).select(T_rate_plan.star).where(T_rate_plan.id == P())
    plan = conn.execute(pq.get_sql(), (rate_plan_id,)).fetchone()
    if not plan:
        raise RatingError(
            f"Rate plan not found: {rate_plan_id} (data error — the "
            f"meter references a rate plan that does not exist)")

    plan_type = plan["plan_type"]
    if plan_type not in VALID_PLAN_TYPES:
        raise RatingError(
            f"Rate plan {rate_plan_id} has unknown plan_type "
            f"'{plan_type}' (data error). Valid: "
            f"{', '.join(VALID_PLAN_TYPES)}")

    tq = (Q.from_(T_rate_tier).select(T_rate_tier.star)
          .where(T_rate_tier.rate_plan_id == P())
          .orderby(T_rate_tier.sort_order))
    tiers = [dict(r) for r in conn.execute(tq.get_sql(), (rate_plan_id,)).fetchall()]

    try:
        # Everything that can hit a data error — aggregation, per-type
        # rating context (TOU partition rows, peak demand, hybrid
        # strategy), the rating itself, and the prepaid deduction —
        # raises RatingError: collected per meter by the billing_run
        # library (failed target + rollback), never aborting the run, and
        # (because all Python-side conversions precede any write) never
        # leaving partial writes.

        # Aggregate consumption in PYTHON, row by row through _dec —
        # never a SQL aggregate (QA round-2 DEFECT-2): SUM over TEXT is
        # float on SQLite and an error on PostgreSQL, and even the
        # decimal_sum custom aggregate makes the STATEMENT the failure
        # point — one unparseable legacy row raised inside the
        # aggregate's step (sqlite3.OperationalError, not RatingError),
        # escaped this per-meter guard, and aborted the whole run (on
        # PG it would additionally poison the transaction). Summing
        # here keeps the failure a contained per-meter RatingError that
        # NAMES the offending row, is exact Decimal, and emits
        # dialect-identical SQL.
        readings_consumption = Decimal("0")
        for r in conn.execute(
                """SELECT id, consumption FROM meter_reading
                   WHERE meter_id = ? AND reading_date >= ?
                   AND reading_date <= ? AND consumption IS NOT NULL""",
                (meter_id, from_date, to_date)).fetchall():
            readings_consumption += _dec(
                r["consumption"], f"meter reading {r['id']} consumption")

        events_consumption = Decimal("0")
        for e in conn.execute(
                """SELECT id, quantity FROM usage_event
                   WHERE meter_id = ? AND processed = 0
                   AND timestamp >= ? AND timestamp <= ?""",
                (meter_id, from_date, to_date)).fetchall():
            events_consumption += _dec(
                e["quantity"] or "0", f"usage event {e['id']} quantity")

        total_consumption = readings_consumption + events_consumption

        usage_rows = None
        peak_demand = None
        peak_demand_str = None
        tier_strategy = None
        prepaid_outcome = None

        if plan_type in ("time_of_use", "hybrid"):
            usage_rows = []
            r_rows = conn.execute(
                """SELECT id, reading_date, consumption FROM meter_reading
                   WHERE meter_id = ? AND reading_date >= ?
                   AND reading_date <= ? AND consumption IS NOT NULL""",
                (meter_id, from_date, to_date)).fetchall()
            for r in r_rows:
                usage_rows.append({
                    "timestamp": r["reading_date"],
                    "quantity": r["consumption"],
                    "source": f"meter reading {r['id']}",
                })
            e_rows = conn.execute(
                """SELECT id, timestamp, quantity FROM usage_event
                   WHERE meter_id = ? AND processed = 0
                   AND timestamp >= ? AND timestamp <= ?""",
                (meter_id, from_date, to_date)).fetchall()
            for e in e_rows:
                usage_rows.append({
                    "timestamp": e["timestamp"],
                    "quantity": e["quantity"],
                    "source": f"usage event {e['id']}",
                })

        if plan_type in ("demand", "hybrid"):
            if existing and existing["peak_demand"] is not None:
                peak_demand = existing["peak_demand"]
            else:
                e_rows = conn.execute(
                    """SELECT quantity FROM usage_event
                       WHERE meter_id = ? AND processed = 0
                       AND timestamp >= ? AND timestamp <= ?""",
                    (meter_id, from_date, to_date)).fetchall()
                # Decimal MAX in Python — a lexical (string) max would
                # pick "9" over "100" (QA D4/M2)
                peaks = [_dec(e["quantity"] or "0",
                              "usage event quantity (peak demand)")
                         for e in e_rows]
                if peaks:
                    peak_demand = str(max(peaks))
            if (plan_type == "demand" and peak_demand is None
                    and total_consumption > 0):
                raise RatingError(
                    "demand rating needs a peak demand but none is "
                    "determinable: no billing_period.peak_demand and no "
                    "usage events in the window (interval samples)")
            if plan_type == "demand" and peak_demand is None:
                peak_demand = "0"

        if peak_demand is not None:
            # Normalize inside the guard: a garbage stored peak_demand
            # is a per-meter data error, not a run abort
            peak_demand_str = str(_dec(
                peak_demand,
                f"stored peak_demand for meter {meter['meter_number']}"))

        if plan_type == "hybrid":
            tier_strategy = _load_tier_strategy(plan)

        # Rate the consumption
        charges = _calculate_charge(
            plan_type, tiers, str(total_consumption),
            base_charge=plan["base_charge"],
            minimum_charge=plan["minimum_charge"],
            usage_events=usage_rows,
            peak_demand=peak_demand,
            tier_strategy=tier_strategy,
        )

        # Prepaid plans: deduct the rated charge from the customer's
        # prepaid_credit_balance inside this same transaction and the
        # same per-meter guard (a garbage legacy credit row is a
        # contained per-meter error; the deduction validates every row
        # before its first UPDATE, so failure means zero writes).
        # Insufficient balance is an explicit over_limit outcome in the
        # run output (the period still rates — the amount is owed;
        # collection is separate).
        if plan_type == "prepaid_credit":
            prepaid_outcome = _apply_prepaid_deduction(
                conn, meter["customer_id"], charges["total_charge"],
                as_of_date=billing_date)
    except RatingError:
        raise
    except ArithmeticError as exc:
        # Defense-in-depth for legacy stored poison (QA round-3): a
        # decimal.InvalidOperation (e.g. quantize overflow on a product
        # or sum of amounts each individually representable) is an
        # ArithmeticError, NOT a RatingError — it must be a contained
        # per-meter data error, never a whole-run abort. Stored single
        # values are already caught earlier as row-naming RatingErrors
        # via _dec; this leg preserves the stage-1 wording for the
        # arithmetic residue before the billing_run library records it.
        raise RatingError(
            f"arithmetic overflow while rating (data error, "
            f"{exc.__class__.__name__}): an amount for this meter "
            f"exceeds the supported currency magnitude")

    usage_charge = charges["usage_charge"]
    base_charge = charges["base_charge"]
    total_charge = charges["total_charge"]
    subtotal = total_charge  # Before adjustments
    demand_charge = charges.get("demand_charge")

    # Create or update billing period
    now = _now()
    if existing and existing["status"] == "open":
        period_id = existing["id"]
        # PM contract item (Wave F S1.3): a re-rate under the meter's
        # CURRENT plan stamps rate_plan_id in the SAME UPDATE — the row
        # must record the plan that actually priced it (a mid-cycle plan
        # change previously left the stale plan id on the rated row).
        uq = (Q.update(T_billing_period)
              .set(T_billing_period.rate_plan_id, P())
              .set(T_billing_period.total_consumption, P())
              .set(T_billing_period.base_charge, P())
              .set(T_billing_period.usage_charge, P())
              .set(T_billing_period.subtotal, P())
              .set(T_billing_period.grand_total, P())
              .set(T_billing_period.peak_demand, P())
              .set(T_billing_period.demand_charge, P())
              .set(T_billing_period.status, ValueWrapper("rated"))
              .set(T_billing_period.rated_at, P())
              .set(T_billing_period.updated_at, P())
              .where(T_billing_period.id == P()))
        conn.execute(uq.get_sql(),
            (rate_plan_id, str(total_consumption), base_charge, usage_charge,
             subtotal, total_charge, peak_demand_str,
             demand_charge, now, now, period_id))
    else:
        period_id = str(uuid.uuid4())
        iq = (Q.into(T_billing_period)
              .columns("id", "customer_id", "meter_id", "rate_plan_id",
                       "period_start", "period_end", "total_consumption",
                       "base_charge", "usage_charge", "adjustments_total",
                       "subtotal", "tax_amount", "grand_total",
                       "peak_demand", "demand_charge", "status",
                       "rated_at", "created_at", "updated_at")
              .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(),
                      ValueWrapper("0"), P(), ValueWrapper("0"), P(),
                      P(), P(), ValueWrapper("rated"), P(), P(), P()))
        conn.execute(iq.get_sql(),
            (period_id, meter["customer_id"], meter_id, rate_plan_id,
             from_date, to_date, str(total_consumption),
             base_charge, usage_charge, subtotal, total_charge,
             peak_demand_str, demand_charge, now, now, now))

    if prepaid_outcome is not None:
        prepaid_outcome["billing_period_id"] = period_id
        prepaid_outcome["meter_id"] = meter_id
        prepaid_outcome["customer_id"] = meter["customer_id"]

    # Mark usage events as processed
    muq = (Q.update(T_usage_event)
           .set(T_usage_event.processed, ValueWrapper(1))
           .set(T_usage_event.billing_period_id, P())
           .where(T_usage_event.meter_id == P())
           .where(T_usage_event.processed == ValueWrapper(0))
           .where(T_usage_event.timestamp >= P())
           .where(T_usage_event.timestamp <= P()))
    conn.execute(muq.get_sql(), (period_id, meter_id, from_date, to_date))

    return {
        "voucher_id": period_id,
        "total_charge": total_charge,
        "prepaid_outcome": prepaid_outcome,
    }


def generate_invoices(conn, args):
    """Create sales invoices from rated billing periods.

    S1.2b (minimal-honest fix, 2026-07-25): a billing period is marked
    'invoiced' ONLY together with a real invoice id. Any failure — selling
    module unavailable, subprocess error/timeout, unparseable output —
    leaves the period 'rated' (retryable) and reports the reason in the
    result. The full per-target structural model arrives with the
    billing_run registry (S1.3).
    """
    bp_ids = _parse_json_arg(args.billing_period_ids, "billing-period-ids")
    if not isinstance(bp_ids, list):
        err("--billing-period-ids must be a JSON array")

    from erpclaw_lib.dependencies import resolve_skill_script, table_exists
    selling_script = resolve_skill_script("erpclaw") if table_exists(conn, "sales_invoice") else None

    results = []
    for bp_id in bp_ids:
        bq = Q.from_(T_billing_period).select(T_billing_period.star).where(T_billing_period.id == P())
        bp = conn.execute(bq.get_sql(), (bp_id,)).fetchone()
        if not bp:
            results.append({"billing_period_id": bp_id, "error": "Not found"})
            continue
        if bp["status"] != "rated":
            results.append({"billing_period_id": bp_id,
                            "error": f"Status is '{bp['status']}', expected 'rated'"})
            continue

        if not selling_script:
            results.append({
                "billing_period_id": bp_id,
                "error": ("Invoice creation unavailable: erpclaw selling "
                          "script not found. The billing period stays "
                          "'rated'; re-run generate-invoices once the "
                          "foundation install is repaired."),
            })
            continue

        invoice_id = None
        failure = None
        try:
            cmd = [
                sys.executable, selling_script,
                "--action", "add-sales-invoice",
                "--customer-id", bp["customer_id"],
                "--items", json.dumps([{
                    "description": (f"Billing period "
                                    f"{bp['period_start']} to {bp['period_end']}"),
                    "qty": "1",
                    "rate": bp["grand_total"],
                }]),
            ]
            # Same DB-context forwarding as resume-billing-run (the child
            # resolves db_path eagerly; without the flag it writes the
            # DEFAULT database).
            _gi_db_path = getattr(args, "db_path", None)
            if _gi_db_path:
                cmd.extend(["--db-path", _gi_db_path])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                detail = (proc.stdout or proc.stderr or "").strip()
                failure = (f"add-sales-invoice exited {proc.returncode}"
                           + (f": {detail[:500]}" if detail else ""))
            else:
                try:
                    inv_result = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    inv_result = None
                    failure = ("add-sales-invoice produced unparseable "
                               f"output: {(proc.stdout or '').strip()[:500]}")
                if inv_result is not None:
                    if inv_result.get("status") == "ok":
                        invoice_id = inv_result.get("sales_invoice", {}).get("id")
                        if not invoice_id:
                            failure = ("add-sales-invoice returned ok but "
                                       "no invoice id")
                    else:
                        failure = (f"add-sales-invoice failed: "
                                   f"{inv_result.get('error', 'unknown error')}")
        except subprocess.TimeoutExpired:
            failure = "add-sales-invoice timed out after 30s"
        except Exception as exc:  # never a silent pass — record and report
            failure = f"invoice creation failed: {exc}"

        if invoice_id is None:
            results.append({
                "billing_period_id": bp_id,
                "error": (failure or "invoice creation failed")
                         + " — billing period left 'rated' for retry",
            })
            continue

        inv_now = _now()
        uq = (Q.update(T_billing_period)
              .set(T_billing_period.status, ValueWrapper("invoiced"))
              .set(T_billing_period.invoiced_at, P())
              .set(T_billing_period.invoice_id, P())
              .set(T_billing_period.updated_at, P())
              .where(T_billing_period.id == P()))
        conn.execute(uq.get_sql(), (inv_now, invoice_id, inv_now, bp_id))
        results.append({
            "billing_period_id": bp_id,
            "invoice_id": invoice_id,
            "status": "invoiced",
        })

    conn.commit()
    invoiced_count = sum(1 for r in results if r.get("status") == "invoiced")
    failed_count = sum(1 for r in results if "error" in r)
    ok({"invoiced": invoiced_count, "failed": failed_count,
         "results": results})


_BILLING_RUN_STATUSES = ("pending", "running", "completed", "failed",
                         "partially_completed")


def list_billing_runs(conn, args):
    """List billing runs with optional status/type/date filters (S1.3)."""
    status = getattr(args, "status", None)
    run_type = getattr(args, "run_type", None)
    if status and status not in _BILLING_RUN_STATUSES:
        err(f"Invalid --status: {status}. "
             f"Must be one of: {', '.join(_BILLING_RUN_STATUSES)}")
    if run_type and run_type not in billing_run_lib.RUN_TYPES:
        err(f"Invalid --run-type: {run_type}. "
             f"Must be one of: {', '.join(billing_run_lib.RUN_TYPES)}")
    limit = int(getattr(args, "limit", None) or 20)
    offset = int(getattr(args, "offset", None) or 0)

    # Query builder (list_billing_periods idiom) — no assembled SQL text:
    # dialect-portable and no f-string SQL site to reason about.
    br = Table("billing_run")
    params = []
    count_q = Q.from_(br).select(fn.Count("*").as_("cnt"))
    data_q = Q.from_(br).select(br.star)

    if status:
        count_q = count_q.where(br.status == P())
        data_q = data_q.where(br.status == P())
        params.append(status)
    if run_type:
        count_q = count_q.where(br.run_type == P())
        data_q = data_q.where(br.run_type == P())
        params.append(run_type)
    if getattr(args, "from_date", None):
        count_q = count_q.where(br.as_of_date >= P())
        data_q = data_q.where(br.as_of_date >= P())
        params.append(args.from_date)
    if getattr(args, "to_date", None):
        count_q = count_q.where(br.as_of_date <= P())
        data_q = data_q.where(br.as_of_date <= P())
        params.append(args.to_date)

    total_count = conn.execute(count_q.get_sql(), params).fetchone()["cnt"]
    data_q = (data_q.orderby(br.created_at, order=Order.desc)
              .orderby(br.id, order=Order.desc).limit(P()).offset(P()))
    rows = conn.execute(data_q.get_sql(), params + [limit, offset]).fetchall()
    ok({"billing_runs": [dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


def get_billing_run(conn, args):
    """Return one billing run's header + every target with its status (S1.3)."""
    run_id = getattr(args, "run_id", None)
    if not run_id:
        err("--run-id is required")
    run = billing_run_lib.load_run(conn, run_id)
    if run is None:
        err(f"Billing run not found: {run_id}")
    targets = billing_run_lib.list_targets(conn, run_id)
    ok({"billing_run": run, "targets": targets,
         "target_count": len(targets)})


def resume_billing_run(conn, args):
    """Resume a crashed/partial billing run (S1.3).

    Dispatches by run_type: usage_billing resumes in-process with the
    run-billing callback; recurring_invoices / recurring_journals are
    owned by erpclaw-selling / erpclaw-journals, so the resume is delegated
    to the owning action (name preserved) via `--resume-run-id` — per-item
    logic never leaves its owning module.
    """
    run_id = getattr(args, "run_id", None)
    if not run_id:
        err("--run-id is required")
    run = billing_run_lib.load_run(conn, run_id)
    if run is None:
        err(f"Billing run not found: {run_id}")
    if run["status"] == "completed":
        err(f"Billing run {run_id} is completed; nothing to resume")

    run_type = run["run_type"]
    if run_type == "usage_billing":
        try:
            params = json.loads(run["params_json"]) if run["params_json"] else {}
        except json.JSONDecodeError:
            params = {}
        from_date = params.get("from_date")
        to_date = params.get("to_date")
        billing_date = params.get("billing_date") or run["as_of_date"]
        if not from_date or not to_date:
            err(f"Billing run {run_id} carries no billing window in "
                 f"params_json; cannot replay it faithfully")
        now = _now()
        conn.execute(
            "UPDATE billing_run SET status = 'running', finished_at = NULL, "
            "updated_at = ? WHERE id = ?", (now, run_id))
        conn.commit()
        state = _run_billing_targets(conn, run_id, from_date, to_date,
                                     billing_date)
        summary = billing_run_lib.finalize(conn, run_id)
        audit(conn, "erpclaw-billing", "resume-billing-run", "billing_run",
              run_id, new_values={"periods": len(state["created_periods"]),
                                  "errors": len(state["errors"])})
        conn.commit()
        result = {
            "billing_run_id": run_id,
            "resumed": True,
            "run_status": summary["status"],
            "periods_created": len(state["created_periods"]),
            "period_ids": state["created_periods"],
            "total_billed": str(round_currency(state["total_billed"])),
            "already_billed": state["already_billed"],
            "error_count": len(state["errors"]),
            "errors": state["errors"],
        }
        if state["prepaid_outcomes"]:
            result["prepaid"] = state["prepaid_outcomes"]
        ok(result)

    if run_type == "combined":
        err("run_type 'combined' has no processor yet (arrives with "
             "F-depth.4); nothing to resume")

    action = {"recurring_invoices": "generate-recurring-invoices",
              "recurring_journals": "process-recurring"}[run_type]
    from erpclaw_lib.dependencies import resolve_skill_script
    script = resolve_skill_script("erpclaw")
    if not script:
        err(f"Cannot resume run {run_id}: the erpclaw foundation script "
             f"that owns '{action}' was not found")
    if not run["company_id"]:
        err(f"Billing run {run_id} carries no company_id; cannot resume")
    cmd = [sys.executable, script, "--action", action,
           "--resume-run-id", run_id,
           "--company-id", run["company_id"]]
    # Forward the DB context: the delegated owners resolve their db_path
    # eagerly (args.db_path or DEFAULT_DB_PATH), which bypasses the
    # ERPCLAW_DB_PATH env fallback — without this flag a delegated resume
    # silently targets the DEFAULT database (QA S1.3 round 1, DEFECT-1;
    # same idiom as _dispatch_dunning_email in erpclaw-selling).
    db_path = getattr(args, "db_path", None)
    if db_path:
        cmd.extend(["--db-path", db_path])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        err(f"Resume of run {run_id} via {action} timed out after 300s; "
             f"re-run resume-billing-run — completed targets are not re-done")
    output = (proc.stdout or "").strip()
    try:
        parsed = json.loads(output) if output else None
    except json.JSONDecodeError:
        parsed = None
    if proc.returncode != 0 or parsed is None:
        detail = output or (proc.stderr or "").strip()
        err(f"Resume of run {run_id} via {action} failed: {detail[:500]}")
    ok({"billing_run_id": run_id, "resumed": True,
         "dispatched_to": action, "result": parsed})


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
    _check_number(args.amount, "--amount")

    q = Q.from_(T_billing_period).select(T_billing_period.star).where(T_billing_period.id == P())
    bp = conn.execute(q.get_sql(), (args.billing_period_id,)).fetchone()
    if not bp:
        err(f"Billing period not found: {args.billing_period_id}")

    adj_id = str(uuid.uuid4())
    q = (Q.into(T_billing_adjustment)
         .columns("id", "billing_period_id", "adjustment_type",
                  "amount", "reason", "approved_by", "created_at")
         .insert(P(), P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(),
        (adj_id, args.billing_period_id, args.adjustment_type,
         args.amount, args.reason, args.approved_by, _now()))

    # Recalculate totals (uses custom decimal_sum aggregate — keep DecimalSum)
    q = (Q.from_(T_billing_adjustment)
         .select(fn.Coalesce(DecimalSum(T_billing_adjustment.amount), ValueWrapper("0")).as_("total"))
         .where(T_billing_adjustment.billing_period_id == P()))
    adj_total_row = conn.execute(q.get_sql(), (args.billing_period_id,)).fetchone()
    adj_total = round_currency(to_decimal(str(adj_total_row["total"])))

    base = to_decimal(bp["base_charge"] or "0")
    usage = to_decimal(bp["usage_charge"] or "0")
    tax = to_decimal(bp["tax_amount"] or "0")
    subtotal = round_currency(base + usage + adj_total)
    grand_total = round_currency(subtotal + tax)

    now = _now()
    q = (Q.update(T_billing_period)
         .set(T_billing_period.adjustments_total, P())
         .set(T_billing_period.subtotal, P())
         .set(T_billing_period.grand_total, P())
         .set(T_billing_period.updated_at, P())
         .where(T_billing_period.id == P()))
    conn.execute(q.get_sql(),
        (str(adj_total), str(subtotal), str(grand_total), now, args.billing_period_id))
    conn.commit()

    audit(conn, "erpclaw-billing", "add-billing-adjustment", "billing_adjustment", adj_id,
           new_values={"amount": args.amount, "type": args.adjustment_type})

    q = Q.from_(T_billing_adjustment).select(T_billing_adjustment.star).where(T_billing_adjustment.id == P())
    adj = row_to_dict(conn.execute(q.get_sql(), (adj_id,)).fetchone())
    adj["updated_grand_total"] = str(grand_total)
    ok({"adjustment": adj})


def list_billing_periods(conn, args):
    """List billing periods with optional filters."""
    bp = Table("billing_period")
    c = Table("customer")
    m = Table("meter")
    params = []
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)

    count_q = Q.from_(bp).select(fn.Count("*").as_("cnt"))
    data_q = (Q.from_(bp)
              .select(bp.star, c.name.as_("customer_name"), m.meter_number)
              .left_join(c).on(bp.customer_id == c.id)
              .left_join(m).on(bp.meter_id == m.id))

    if args.customer_id:
        count_q = count_q.where(bp.customer_id == P())
        data_q = data_q.where(bp.customer_id == P())
        params.append(args.customer_id)
    if args.meter_id:
        count_q = count_q.where(bp.meter_id == P())
        data_q = data_q.where(bp.meter_id == P())
        params.append(args.meter_id)
    if args.status:
        count_q = count_q.where(bp.status == P())
        data_q = data_q.where(bp.status == P())
        params.append(args.status)
    if args.from_date:
        count_q = count_q.where(bp.period_start >= P())
        data_q = data_q.where(bp.period_start >= P())
        params.append(args.from_date)
    if args.to_date:
        count_q = count_q.where(bp.period_end <= P())
        data_q = data_q.where(bp.period_end <= P())
        params.append(args.to_date)

    total_count = conn.execute(count_q.get_sql(), params).fetchone()["cnt"]

    data_q = data_q.orderby(bp.created_at, order=Order.desc).limit(P()).offset(P())
    rows = conn.execute(data_q.get_sql(), params + [limit, offset]).fetchall()
    ok({"billing_periods": [dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


def get_billing_period(conn, args):
    """Get billing period with adjustments."""
    if not args.billing_period_id:
        err("--billing-period-id is required")

    bpt = Table("billing_period")
    c = Table("customer")
    m = Table("meter")
    rp = Table("rate_plan")
    q = (Q.from_(bpt)
         .select(bpt.star, c.name.as_("customer_name"),
                 m.meter_number, rp.name.as_("rate_plan_name"))
         .left_join(c).on(bpt.customer_id == c.id)
         .left_join(m).on(bpt.meter_id == m.id)
         .left_join(rp).on(bpt.rate_plan_id == rp.id)
         .where(bpt.id == P()))
    bp = conn.execute(q.get_sql(), (args.billing_period_id,)).fetchone()
    if not bp:
        err(f"Billing period not found: {args.billing_period_id}")

    result = row_to_dict(bp)
    q = (Q.from_(T_billing_adjustment).select(T_billing_adjustment.star)
         .where(T_billing_adjustment.billing_period_id == P())
         .orderby(T_billing_adjustment.created_at))
    adjustments = [dict(r) for r in conn.execute(q.get_sql(), (args.billing_period_id,)).fetchall()]
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
    amount_dec = _check_number(args.amount, "--amount")
    if amount_dec <= 0:
        err(f"--amount must be positive, got: {args.amount}")
    if not args.valid_until:
        err("--valid-until is required")

    q = Q.from_(T_customer).select(T_customer.id).where(T_customer.id == P())
    cust = conn.execute(q.get_sql(), (args.customer_id,)).fetchone()
    if not cust:
        err(f"Customer not found: {args.customer_id}")

    rate_plan_id = args.rate_plan_id
    if rate_plan_id:
        q = Q.from_(T_rate_plan).select(T_rate_plan.id).where(T_rate_plan.id == P())
        rp = conn.execute(q.get_sql(), (rate_plan_id,)).fetchone()
        if not rp:
            err(f"Rate plan not found: {rate_plan_id}")
    else:
        # Find any rate plan with type prepaid_credit, or use first available
        q = (Q.from_(T_rate_plan).select(T_rate_plan.id)
             .where(T_rate_plan.plan_type == ValueWrapper("prepaid_credit"))
             .limit(1))
        rp = conn.execute(q.get_sql()).fetchone()
        if not rp:
            q = Q.from_(T_rate_plan).select(T_rate_plan.id).limit(1)
            rp = conn.execute(q.get_sql()).fetchone()
        if not rp:
            err("No rate plan available. Create one first")
        rate_plan_id = rp["id"]

    period_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    amount = args.amount
    now = _now()

    credit_id = str(uuid.uuid4())
    q = (Q.into(T_prepaid)
         .columns("id", "customer_id", "rate_plan_id", "original_amount",
                  "remaining_amount", "period_start", "period_end",
                  "overage_amount", "status", "created_at", "updated_at")
         .insert(P(), P(), P(), P(), P(), P(), P(),
                 ValueWrapper("0"), ValueWrapper("active"), P(), P()))
    conn.execute(q.get_sql(),
        (credit_id, args.customer_id, rate_plan_id,
         amount, amount, period_start, args.valid_until, now, now))
    conn.commit()
    audit(conn, "erpclaw-billing", "add-prepaid-credit", "prepaid_credit_balance", credit_id,
           new_values={"amount": amount, "valid_until": args.valid_until})

    q = Q.from_(T_prepaid).select(T_prepaid.star).where(T_prepaid.id == P())
    credit = row_to_dict(conn.execute(q.get_sql(), (credit_id,)).fetchone())
    ok({"prepaid_credit": credit})


def get_prepaid_balance(conn, args):
    """Check remaining prepaid credits for a customer."""
    if not args.customer_id:
        err("--customer-id is required")

    q = (Q.from_(T_prepaid).select(T_prepaid.star)
         .where(T_prepaid.customer_id == P())
         .orderby(T_prepaid.created_at, order=Order.desc))
    rows = conn.execute(q.get_sql(), (args.customer_id,)).fetchall()

    balances = [dict(r) for r in rows]
    total_remaining = Decimal("0")
    active_count = 0
    for b in balances:
        if b["status"] == "active":
            # Legacy poisoned rows (garbage, non-finite, or unrepresentable
            # magnitude, QA round-3) must produce a NAMED data error
            # identifying the offending row — never the generic message.
            # Recovery is a manual repair of that row (raw SQL); write
            # gates prevent new poison from entering.
            try:
                total_remaining += _finite_decimal(
                    b["remaining_amount"] or "0")
            except (TypeError, ValueError, ArithmeticError):
                err(f"prepaid credit {b['id']} has an invalid "
                     f"remaining_amount: {b['remaining_amount']!r} (data "
                     f"error). Repair this prepaid_credit_balance row "
                     f"before reading the balance.")
            active_count += 1

    try:
        total_str = str(round_currency(total_remaining))
    except ArithmeticError:
        err(f"total prepaid balance for customer {args.customer_id} "
             f"exceeds the supported currency magnitude (data error). "
             f"Repair the oversized prepaid_credit_balance rows.")

    ok({
        "customer_id": args.customer_id,
        "active_credits": active_count,
        "total_remaining": total_str,
        "balances": balances,
    })


# =========================================================================
# STATUS (action 22)
# =========================================================================

def status_action(conn, args):
    """Billing summary."""
    # Dynamic IN clauses — keep as raw SQL for variable-length placeholders
    where = ""
    params = []
    cust_where = ""

    if args.company_id:
        q = Q.from_(T_company).select(T_company.id).where(T_company.id == P())
        company = conn.execute(q.get_sql(), (args.company_id,)).fetchone()
        if not company:
            err(f"Company not found: {args.company_id}")
        cq = Q.from_(T_customer).select(T_customer.id).where(T_customer.company_id == P())
        cust_ids = [r["id"] for r in conn.execute(cq.get_sql(), (args.company_id,)).fetchall()]
        if cust_ids:
            placeholders = ",".join("?" * len(cust_ids))
            where = f"WHERE customer_id IN ({placeholders})"
            params = cust_ids
            cust_where = where

    # Meter counts by status (dynamic IN — raw SQL)
    meter_q = f"SELECT status, COUNT(*) as cnt FROM meter {where} GROUP BY status"
    meter_counts = {}
    for row in conn.execute(meter_q, params).fetchall():
        meter_counts[row["status"]] = row["cnt"]

    # Billing period counts by status (dynamic IN — raw SQL)
    bp_q = f"SELECT status, COUNT(*) as cnt FROM billing_period {cust_where} GROUP BY status"
    bp_counts = {}
    for row in conn.execute(bp_q, params).fetchall():
        bp_counts[row["status"]] = row["cnt"]

    # Unprocessed usage events (dynamic IN — raw SQL)
    evt_q = "SELECT COUNT(*) as cnt FROM usage_event WHERE processed = 0"
    if params:
        evt_q += f" AND customer_id IN ({','.join('?' * len(params))})"
    unprocessed = conn.execute(evt_q, params).fetchone()["cnt"]

    # Rate plans count
    rp_q = Q.from_(T_rate_plan).select(fn.Count("*").as_("cnt"))
    rp_count = conn.execute(rp_q.get_sql()).fetchone()["cnt"]

    # Prepaid balance count (dynamic IN — raw SQL)
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
    "list-billing-runs": list_billing_runs,
    "get-billing-run": get_billing_run,
    "resume-billing-run": resume_billing_run,
    "status": status_action,
}


def main():
    parser = SafeArgumentParser(description="ERPClaw Billing")
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
    parser.add_argument("--tier-strategy")     # hybrid plans: components JSON
    parser.add_argument("--usage-by-period")   # TOU rate-consumption: {"peak": "120", ...}
    parser.add_argument("--peak-demand")       # demand rate-consumption: kW
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
    # Billing-run registry fields (S1.3)
    parser.add_argument("--run-id")
    parser.add_argument("--run-type")
    # Filters
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")

    args, unknown = parser.parse_known_args()
    check_unknown_args(parser, unknown)
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
