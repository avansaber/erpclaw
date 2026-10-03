"""ERPClaw Advanced Accounting -- Revenue Recognition (ASC 606) domain module

Actions for revenue contracts, performance obligations, variable consideration,
and revenue schedules (4 tables, 14 actions).
Imported by db_query.py (unified router).
"""
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

try:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.naming import get_next_name, ENTITY_PREFIXES
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.query import Case, DecimalSum, Field, P, Q, Table, dynamic_update, fn, update_row
    from erpclaw_lib.query_helpers import resolve_company_id, resolve_scope_company
    from erpclaw_lib.gl_posting import insert_gl_entries

    ENTITY_PREFIXES.setdefault("revenue_contract", "RCON-")
except ImportError:
    pass

SKILL = "erpclaw-accounting-adv"

RECOGNITION_VOUCHER_TYPE = "revenue_recognition"

_now_iso = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------
VALID_CONTRACT_STATUSES = ("draft", "active", "modified", "completed", "terminated")
VALID_RECOGNITION_METHODS = ("point_in_time", "over_time")
VALID_RECOGNITION_BASES = ("output", "input", "time")
VALID_OBLIGATION_STATUSES = ("unsatisfied", "partially_satisfied", "satisfied")
VALID_VC_METHODS = ("expected_value", "most_likely")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _validate_company(conn, company_id):
    if not company_id:
        err("--company-id is required")
    if not conn.execute("SELECT id FROM company WHERE id = ?", (company_id,)).fetchone():
        err(f"Company {company_id} not found")


def _money(raw):
    """Parse a money amount: finite, non-negative sign unchecked, at most 2dp.

    Returns the Decimal when raw parses, is finite, and carries an exponent
    between -2 and 0 inclusive. Otherwise returns None. Callers check the
    sign. ``is_finite`` is tested before reading the exponent because NaN
    and Infinity carry a non-integer exponent that cannot be compared.
    """
    try:
        amount = Decimal(raw)
    except Exception:
        return None
    if not amount.is_finite():
        return None
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2 or exponent > 0:
        return None
    return amount


def _recognition_accounts(conn, args, company_id):
    """Resolve the ledger accounts a recognition posting needs, before any write."""
    deferred_id = getattr(args, "deferred_revenue_account_id", None)
    revenue_id = getattr(args, "revenue_account_id", None)
    cost_center_id = getattr(args, "cost_center_id", None)
    currency = getattr(args, "currency", None) or "USD"
    if not deferred_id or not revenue_id:
        err("--deferred-revenue-account-id and --revenue-account-id are required: "
            "recognizing revenue posts DR deferred revenue / CR revenue")
    acct_t = Table("account")
    deferred_row = conn.execute(
        Q.from_(acct_t).select(acct_t.star)
        .where(acct_t.id == P()).where(acct_t.company_id == P()).get_sql(),
        (deferred_id, company_id)).fetchone()
    if deferred_row is None:
        err(f"Account {deferred_id} not found in company {company_id}")
    revenue_row = conn.execute(
        Q.from_(acct_t).select(acct_t.star)
        .where(acct_t.id == P()).where(acct_t.company_id == P()).get_sql(),
        (revenue_id, company_id)).fetchone()
    if revenue_row is None:
        err(f"Account {revenue_id} not found in company {company_id}")
    deferred = row_to_dict(deferred_row)
    revenue = row_to_dict(revenue_row)
    if deferred.get("root_type") != "liability":
        err(f"Deferred revenue account {deferred_id} must be a liability account, "
            f"not {deferred.get('root_type')}")
    if revenue.get("root_type") != "income":
        err(f"Revenue account {revenue_id} must be an income account, "
            f"not {revenue.get('root_type')}")
    return {
        "deferred_revenue_account_id": deferred_id,
        "revenue_account_id": revenue_id,
        "cost_center_id": cost_center_id,
        "currency": currency,
    }


def _post_recognition(conn, entry, accts):
    """Post DR deferred / CR revenue for one schedule entry and flag it.

    Runs inside the caller's transaction and never commits: either the
    ledger voucher and the recognized flag land together or neither does.
    Returns the new gl_entry ids.
    """
    raw = entry["amount"]
    amount = _money(raw)
    if amount is None or amount <= 0:
        raise ValueError(
            f"Revenue schedule entry {entry['id']} has amount {raw}; "
            "only a positive two-decimal amount can be recognized")
    currency = accts["currency"]
    legs = [
        {"account_id": accts["deferred_revenue_account_id"],
         "debit": str(amount), "credit": "0",
         "currency": currency, "exchange_rate": "1"},
        {"account_id": accts["revenue_account_id"],
         "debit": "0", "credit": str(amount),
         "cost_center_id": accts["cost_center_id"],
         "currency": currency, "exchange_rate": "1"},
    ]
    gl_ids = insert_gl_entries(
        conn, legs,
        voucher_type=RECOGNITION_VOUCHER_TYPE, voucher_id=entry["id"],
        posting_date=entry["period_date"], company_id=entry["company_id"],
        remarks=f"Revenue recognition {entry['period_date']}")
    flag_sql = update_row(
        "advacct_revenue_schedule",
        data={"recognized": 1}, where={"id": P(), "recognized": 0})
    cursor = conn.execute(flag_sql, (entry["id"],))
    if cursor.rowcount != 1:
        raise ValueError(
            f"Revenue schedule entry {entry['id']} is already recognized")
    return gl_ids


def _validate_contract(conn, contract_id):
    if not contract_id:
        err("--contract-id is required")
    row = conn.execute("SELECT id FROM advacct_revenue_contract WHERE id = ?", (contract_id,)).fetchone()
    if not row:
        err(f"Revenue contract {contract_id} not found")


# ===========================================================================
# 1. add-revenue-contract
# ===========================================================================
def add_revenue_contract(conn, args):
    _validate_company(conn, args.company_id)
    customer_name = getattr(args, "customer_name", None)
    if not customer_name:
        err("--customer-name is required")

    total_value = getattr(args, "total_value", None) or "0"
    parsed_total_value = _money(total_value)
    if parsed_total_value is None or parsed_total_value < 0:
        err(f"Invalid total-value: {total_value}")

    contract_id = str(uuid.uuid4())
    conn.company_id = args.company_id
    naming = get_next_name(conn, "revenue_contract", company_id=args.company_id)
    now = _now_iso()

    conn.execute("""
        INSERT INTO advacct_revenue_contract (
            id, naming_series, customer_name, contract_number, start_date, end_date,
            total_value, allocated_value, contract_status, modification_count,
            company_id, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        contract_id, naming, customer_name,
        getattr(args, "contract_number", None),
        getattr(args, "start_date", None),
        getattr(args, "end_date", None),
        total_value, "0", "draft", 0,
        args.company_id, now, now,
    ))
    audit(conn, SKILL, "add-revenue-contract", "advacct_revenue_contract", contract_id,
          new_values={"customer_name": customer_name, "total_value": total_value})
    conn.commit()
    ok({
        "id": contract_id, "naming_series": naming,
        "customer_name": customer_name, "contract_status": "draft",
        "total_value": total_value,
    })


# ===========================================================================
# 2. update-revenue-contract
# ===========================================================================
def update_revenue_contract(conn, args):
    contract_id = getattr(args, "id", None)
    if not contract_id:
        err("--id is required")
    if not conn.execute("SELECT id FROM advacct_revenue_contract WHERE id = ?", (contract_id,)).fetchone():
        err(f"Revenue contract {contract_id} not found")
    total_value = getattr(args, "total_value", None)
    if total_value is not None:
        parsed_total_value = _money(total_value)
        if parsed_total_value is None or parsed_total_value < 0:
            err(f"Invalid total-value: {total_value}")

    data, changed = {}, []
    for arg_name, col_name in {
        "customer_name": "customer_name",
        "contract_number": "contract_number",
        "start_date": "start_date",
        "end_date": "end_date",
        "total_value": "total_value",
    }.items():
        val = getattr(args, arg_name, None)
        if val is not None:
            data[col_name] = val
            changed.append(col_name)

    contract_status = getattr(args, "contract_status", None)
    if contract_status:
        if contract_status not in VALID_CONTRACT_STATUSES:
            err(f"Invalid contract-status: {contract_status}. Must be one of: {', '.join(VALID_CONTRACT_STATUSES)}")
        data["contract_status"] = contract_status
        changed.append("contract_status")

    if not data:
        err("No fields to update")

    current = row_to_dict(conn.execute(
        "SELECT * FROM advacct_revenue_contract WHERE id = ?", (contract_id,)).fetchone())
    old_values = {col: current[col] for col in changed}
    data["updated_at"] = _now_iso()
    sql, params = dynamic_update("advacct_revenue_contract", data, where={"id": contract_id})
    conn.execute(sql, params)
    new_values = {col: data[col] for col in changed}
    audit(conn, SKILL, "update-revenue-contract", "advacct_revenue_contract", contract_id,
          old_values=old_values, new_values=new_values)
    conn.commit()
    ok({"id": contract_id, "updated_fields": changed})


# ===========================================================================
# 3. get-revenue-contract
# ===========================================================================
def get_revenue_contract(conn, args):
    contract_id = getattr(args, "id", None)
    if not contract_id:
        err("--id is required")
    row = conn.execute("SELECT * FROM advacct_revenue_contract WHERE id = ?", (contract_id,)).fetchone()
    if not row:
        err(f"Revenue contract {contract_id} not found")
    data = row_to_dict(row)

    # Include obligations
    obligations = conn.execute(
        "SELECT * FROM advacct_performance_obligation WHERE contract_id = ? ORDER BY created_at",
        (contract_id,)
    ).fetchall()
    data["obligations"] = [row_to_dict(o) for o in obligations]
    data["obligation_count"] = len(obligations)

    # Include variable considerations
    vcs = conn.execute(
        "SELECT * FROM advacct_variable_consideration WHERE contract_id = ? ORDER BY created_at",
        (contract_id,)
    ).fetchall()
    data["variable_considerations"] = [row_to_dict(v) for v in vcs]

    ok(data)


# ===========================================================================
# 4. list-revenue-contracts
# ===========================================================================
def list_revenue_contracts(conn, args):
    company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))
    where, params = ["1=1"], []
    where.append("company_id = ?")
    params.append(company_id)
    if getattr(args, "contract_status", None):
        where.append("contract_status = ?")
        params.append(args.contract_status)
    if getattr(args, "search", None):
        where.append("(LOWER(customer_name) LIKE LOWER(?) OR LOWER(contract_number) LIKE LOWER(?))")
        params.extend([f"%{args.search}%", f"%{args.search}%"])

    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM advacct_revenue_contract WHERE {where_sql}", params
    ).fetchone()[0]
    params.extend([args.limit, args.offset])
    rows = conn.execute(
        f"SELECT * FROM advacct_revenue_contract WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params
    ).fetchall()
    ok({
        "rows": [row_to_dict(r) for r in rows],
        "total_count": total, "limit": args.limit, "offset": args.offset,
        "has_more": (args.offset + args.limit) < total,
    })


# ===========================================================================
# 5. add-performance-obligation
# ===========================================================================
def add_performance_obligation(conn, args):
    contract_id = getattr(args, "contract_id", None)
    _validate_contract(conn, contract_id)
    _validate_company(conn, args.company_id)

    name = getattr(args, "name", None)
    if not name:
        err("--name is required")

    standalone_price = getattr(args, "standalone_price", None) or "0"
    recognition_method = getattr(args, "recognition_method", None) or "over_time"
    if recognition_method not in VALID_RECOGNITION_METHODS:
        err(f"Invalid recognition-method: {recognition_method}. Must be one of: {', '.join(VALID_RECOGNITION_METHODS)}")

    recognition_basis = getattr(args, "recognition_basis", None) or "time"
    if recognition_basis not in VALID_RECOGNITION_BASES:
        err(f"Invalid recognition-basis: {recognition_basis}. Must be one of: {', '.join(VALID_RECOGNITION_BASES)}")

    parsed_standalone_price = _money(standalone_price)
    if parsed_standalone_price is None or parsed_standalone_price < 0:
        err(f"Invalid standalone-price: {standalone_price}")

    obligation_id = str(uuid.uuid4())
    now = _now_iso()

    # Auto-allocate price: if standalone_price provided, use it as allocated_price
    allocated_price = standalone_price

    conn.execute("""
        INSERT INTO advacct_performance_obligation (
            id, contract_id, name, standalone_price, allocated_price,
            recognition_method, recognition_basis, pct_complete,
            obligation_status, satisfied_date, company_id, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        obligation_id, contract_id, name, standalone_price, allocated_price,
        recognition_method, recognition_basis, "0",
        "unsatisfied", None, args.company_id, now, now,
    ))

    # Update contract allocated_value
    _ob_t = Table("advacct_performance_obligation")
    _alloc_q = (
        Q.from_(_ob_t)
        .select(fn.Coalesce(DecimalSum(_ob_t.allocated_price), "0").as_("total"))
        .where(_ob_t.contract_id == P())
    )
    _alloc_row = conn.execute(_alloc_q.get_sql(), (contract_id,)).fetchone()
    _alloc_raw = _alloc_row["total"] if _alloc_row["total"] is not None else "0"
    total_allocated = str(Decimal(str(_alloc_raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    conn.execute(
        "UPDATE advacct_revenue_contract SET allocated_value = ?, updated_at = ? WHERE id = ?",
        (total_allocated, _now_iso(), contract_id)
    )

    audit(conn, SKILL, "add-performance-obligation", "advacct_performance_obligation", obligation_id,
          new_values={"contract_id": contract_id, "name": name, "standalone_price": standalone_price})
    conn.commit()
    ok({
        "id": obligation_id, "contract_id": contract_id, "name": name,
        "standalone_price": standalone_price, "allocated_price": allocated_price,
        "recognition_method": recognition_method, "obligation_status": "unsatisfied",
    })


# ===========================================================================
# 6. list-performance-obligations
# ===========================================================================
def list_performance_obligations(conn, args):
    if not (getattr(args, "contract_id", None) and not getattr(args, "company_id", None) and not getattr(args, "company_name", None)):
        company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))
    else:
        company_id = None
    where, params = ["1=1"], []
    if getattr(args, "contract_id", None):
        where.append("contract_id = ?")
        params.append(args.contract_id)
    if company_id is not None:
        where.append("company_id = ?")
        params.append(company_id)
    if getattr(args, "obligation_status", None):
        where.append("obligation_status = ?")
        params.append(args.obligation_status)

    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM advacct_performance_obligation WHERE {where_sql}", params
    ).fetchone()[0]
    params.extend([args.limit, args.offset])
    rows = conn.execute(
        f"SELECT * FROM advacct_performance_obligation WHERE {where_sql} ORDER BY created_at LIMIT ? OFFSET ?",
        params
    ).fetchall()
    ok({
        "rows": [row_to_dict(r) for r in rows],
        "total_count": total, "limit": args.limit, "offset": args.offset,
        "has_more": (args.offset + args.limit) < total,
    })


# ===========================================================================
# 7. satisfy-performance-obligation
# ===========================================================================
def satisfy_performance_obligation(conn, args):
    ob_id = getattr(args, "id", None)
    if not ob_id:
        err("--id is required")
    row = conn.execute("SELECT * FROM advacct_performance_obligation WHERE id = ?", (ob_id,)).fetchone()
    if not row:
        err(f"Performance obligation {ob_id} not found")

    data = row_to_dict(row)
    if data["obligation_status"] == "satisfied":
        err("Performance obligation is already satisfied")

    pct_complete = getattr(args, "pct_complete", None) or "100"
    try:
        pct = Decimal(pct_complete)
    except Exception:
        err(f"Invalid pct-complete: {pct_complete}")

    if not pct.is_finite():
        err(f"Invalid pct-complete: {pct_complete}")

    if pct < Decimal("0") or pct > Decimal("100"):
        err("pct-complete must be between 0 and 100")

    now = _now_iso()
    if pct == Decimal("100"):
        new_status = "satisfied"
        satisfied_date = now
    elif pct > Decimal("0"):
        new_status = "partially_satisfied"
        satisfied_date = None
    else:
        new_status = "unsatisfied"
        satisfied_date = None

    conn.execute("""
        UPDATE advacct_performance_obligation
        SET pct_complete = ?, obligation_status = ?, satisfied_date = ?, updated_at = ?
        WHERE id = ?
    """, (str(pct), new_status, satisfied_date, now, ob_id))

    audit(conn, SKILL, "satisfy-performance-obligation", "advacct_performance_obligation", ob_id,
          new_values={"pct_complete": str(pct), "obligation_status": new_status})
    conn.commit()
    ok({"id": ob_id, "pct_complete": str(pct), "obligation_status": new_status})


# ===========================================================================
# 8. add-variable-consideration
# ===========================================================================
def add_variable_consideration(conn, args):
    contract_id = getattr(args, "contract_id", None)
    _validate_contract(conn, contract_id)
    _validate_company(conn, args.company_id)

    description = getattr(args, "description", None)
    if not description:
        err("--description is required")

    estimated_amount = getattr(args, "estimated_amount", None) or "0"
    constraint_amount = getattr(args, "constraint_amount", None) or "0"
    method = getattr(args, "method", None) or "expected_value"
    if method not in VALID_VC_METHODS:
        err(f"Invalid method: {method}. Must be one of: {', '.join(VALID_VC_METHODS)}")

    probability = getattr(args, "probability", None) or "0"

    parsed_estimated_amount = _money(estimated_amount)
    if parsed_estimated_amount is None or parsed_estimated_amount < 0:
        err(f"Invalid estimated-amount: {estimated_amount}")
    parsed_constraint_amount = _money(constraint_amount)
    if parsed_constraint_amount is None or parsed_constraint_amount < 0:
        err(f"Invalid constraint-amount: {constraint_amount}")
    try:
        parsed_probability = Decimal(probability)
    except Exception:
        err(f"Invalid probability: {probability}")
    if (not parsed_probability.is_finite()
            or parsed_probability < Decimal("0")
            or parsed_probability > Decimal("100")):
        err(f"Invalid probability: {probability}")

    vc_id = str(uuid.uuid4())
    now = _now_iso()

    conn.execute("""
        INSERT INTO advacct_variable_consideration (
            id, contract_id, description, estimated_amount, constraint_amount,
            method, probability, company_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        vc_id, contract_id, description, estimated_amount, constraint_amount,
        method, probability, args.company_id, now,
    ))
    audit(conn, SKILL, "add-variable-consideration", "advacct_variable_consideration", vc_id,
          new_values={"contract_id": contract_id, "description": description})
    conn.commit()
    ok({
        "id": vc_id, "contract_id": contract_id, "description": description,
        "estimated_amount": estimated_amount, "method": method,
    })


# ===========================================================================
# 9. list-variable-considerations
# ===========================================================================
def list_variable_considerations(conn, args):
    if not (getattr(args, "contract_id", None) and not getattr(args, "company_id", None) and not getattr(args, "company_name", None)):
        company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))
    else:
        company_id = None
    where, params = ["1=1"], []
    if getattr(args, "contract_id", None):
        where.append("contract_id = ?")
        params.append(args.contract_id)
    if company_id is not None:
        where.append("company_id = ?")
        params.append(company_id)

    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM advacct_variable_consideration WHERE {where_sql}", params
    ).fetchone()[0]
    params.extend([args.limit, args.offset])
    rows = conn.execute(
        f"SELECT * FROM advacct_variable_consideration WHERE {where_sql} ORDER BY created_at LIMIT ? OFFSET ?",
        params
    ).fetchall()
    ok({
        "rows": [row_to_dict(r) for r in rows],
        "total_count": total, "limit": args.limit, "offset": args.offset,
        "has_more": (args.offset + args.limit) < total,
    })


# ===========================================================================
# 10. modify-contract
# ===========================================================================
def modify_contract(conn, args):
    contract_id = getattr(args, "id", None)
    if not contract_id:
        err("--id is required")
    row = conn.execute("SELECT * FROM advacct_revenue_contract WHERE id = ?", (contract_id,)).fetchone()
    if not row:
        err(f"Revenue contract {contract_id} not found")

    data = row_to_dict(row)
    if data["contract_status"] not in ("draft", "active"):
        err(f"Cannot modify contract in status '{data['contract_status']}'. Must be draft or active.")

    now = _now_iso()
    new_count = data["modification_count"] + 1
    conn.execute("""
        UPDATE advacct_revenue_contract
        SET contract_status = 'modified', modification_count = ?, updated_at = ?
        WHERE id = ?
    """, (new_count, now, contract_id))

    audit(conn, SKILL, "modify-contract", "advacct_revenue_contract", contract_id,
          new_values={"contract_status": "modified", "modification_count": new_count})
    conn.commit()
    ok({"id": contract_id, "contract_status": "modified", "modification_count": new_count})


# ===========================================================================
# 11. calculate-revenue-schedule
# ===========================================================================
def calculate_revenue_schedule(conn, args):
    ob_id = getattr(args, "obligation_id", None)
    if not ob_id:
        err("--obligation-id is required")
    row = conn.execute("SELECT * FROM advacct_performance_obligation WHERE id = ?", (ob_id,)).fetchone()
    if not row:
        err(f"Performance obligation {ob_id} not found")

    ob = row_to_dict(row)

    # Get contract for dates
    contract = conn.execute(
        "SELECT * FROM advacct_revenue_contract WHERE id = ?", (ob["contract_id"],)
    ).fetchone()
    if not contract:
        err("Associated contract not found")
    contract_data = row_to_dict(contract)

    start_date = contract_data.get("start_date")
    end_date = contract_data.get("end_date")
    if not start_date or not end_date:
        err("Contract must have start_date and end_date to calculate schedule")

    allocated_price = Decimal(ob["allocated_price"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if allocated_price <= 0:
        err("Allocated price must be greater than zero")

    # Delete existing schedule entries for this obligation
    conn.execute("DELETE FROM advacct_revenue_schedule WHERE obligation_id = ?", (ob_id,))

    # Calculate monthly schedule
    from datetime import date
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    # Count months
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    if months <= 0:
        err("End date must be after start date")

    monthly_amount = (allocated_price / Decimal(str(months))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    remainder = allocated_price - (monthly_amount * (months - 1))

    entries_created = 0
    company_id = ob["company_id"]
    now = _now_iso()

    current_year = start.year
    current_month = start.month

    for i in range(months):
        period_date = f"{current_year}-{current_month:02d}-01"
        amount = str(remainder) if i == months - 1 else str(monthly_amount)

        conn.execute("""
            INSERT INTO advacct_revenue_schedule (
                id, obligation_id, period_date, amount, recognized, company_id, created_at
            ) VALUES (?,?,?,?,?,?,?)
        """, (str(uuid.uuid4()), ob_id, period_date, amount, 0, company_id, now))
        entries_created += 1

        current_month += 1
        if current_month > 12:
            current_month = 1
            current_year += 1

    audit(conn, SKILL, "calculate-revenue-schedule", "advacct_revenue_schedule", ob_id,
          new_values={"entries_created": entries_created, "total_amount": str(allocated_price)})
    conn.commit()
    ok({
        "obligation_id": ob_id, "entries_created": entries_created,
        "monthly_amount": str(monthly_amount), "total_amount": str(allocated_price),
    })


# ===========================================================================
# 12. generate-revenue-entries
# ===========================================================================
def generate_revenue_entries(conn, args):
    ob_id = getattr(args, "obligation_id", None)
    if not ob_id:
        err("--obligation-id is required")
    row = conn.execute("SELECT * FROM advacct_performance_obligation WHERE id = ?", (ob_id,)).fetchone()
    if not row:
        err(f"Performance obligation {ob_id} not found")

    today = _today_utc()
    raw_as_of = getattr(args, "as_of_date", None)
    as_of = raw_as_of if raw_as_of else today
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", as_of):
        err("--as-of-date must be a date in YYYY-MM-DD form")
    try:
        datetime.strptime(as_of, "%Y-%m-%d")
    except ValueError:
        err("--as-of-date must be a date in YYYY-MM-DD form")
    if raw_as_of and as_of > today:
        err(f"--as-of-date {as_of} is later than today ({today}); "
            "revenue for a period that has not arrived is not recognized")

    # Get the unrecognized schedule entries due on or before the as-of date.
    # period_date is stored as YYYY-MM-DD text, so the comparison is a
    # string comparison, as the existing ORDER BY already relies on.
    sched_t = Table("advacct_revenue_schedule")
    due_rows = conn.execute(
        (Q.from_(sched_t).select(sched_t.star)
         .where(sched_t.obligation_id == P())
         .where(sched_t.recognized == 0)
         .where(sched_t.period_date <= P())
         .orderby(sched_t.period_date)
         .orderby(sched_t.id)).get_sql(),
        (ob_id, as_of)).fetchall()

    if not due_rows:
        earliest_rows = conn.execute(
            (Q.from_(sched_t).select(sched_t.star)
             .where(sched_t.obligation_id == P())
             .where(sched_t.recognized == 0)
             .orderby(sched_t.period_date)
             .orderby(sched_t.id)).get_sql(),
            (ob_id,)).fetchall()
        if not earliest_rows:
            err("No unrecognized revenue schedule entries found")
        next_date = row_to_dict(earliest_rows[0])["period_date"]
        err(f"No revenue schedule entries for obligation {ob_id} are due on or before {as_of}; "
            f"the next is {next_date}")

    obligation = row_to_dict(row)
    accts = _recognition_accounts(conn, args, obligation["company_id"])

    entries = [row_to_dict(sched) for sched in due_rows]
    for entry in entries:
        if _money(entry["amount"]) is None or _money(entry["amount"]) <= 0:
            err(f"Revenue for obligation {ob_id} was not recognized: "
                f"schedule entry {entry['id']} has amount {entry['amount']}; "
                "only a positive two-decimal amount can be recognized")

    recognized_count = 0
    total_recognized = Decimal("0")
    gl_entry_count = 0

    for entry in entries:
        try:
            gl_ids = _post_recognition(conn, entry, accts)
        except (ValueError, NotImplementedError) as exc:
            conn.rollback()
            err(f"Revenue for obligation {ob_id} was not recognized: "
                f"the ledger posting for schedule entry {entry['id']} was refused: {exc}")
        recognized_count += 1
        total_recognized += _money(entry["amount"])
        gl_entry_count += len(gl_ids)

    remaining_count = int(conn.execute(
        (Q.from_(sched_t).select(fn.Count("*").as_("remaining"))
         .where(sched_t.obligation_id == P())
         .where(sched_t.recognized == 0)).get_sql(),
        (ob_id,)).fetchone()[0])

    audit(conn, SKILL, "generate-revenue-entries", "advacct_revenue_schedule", ob_id,
          new_values={"recognized_count": recognized_count, "total_recognized": str(total_recognized),
                      "as_of_date": as_of})
    conn.commit()
    ok({
        "obligation_id": ob_id,
        "recognized_count": recognized_count,
        "total_recognized": str(total_recognized),
        "gl_entry_count": gl_entry_count,
        "as_of_date": as_of,
        "remaining_count": remaining_count,
    })


# ===========================================================================
# 13. revenue-waterfall-report
# ===========================================================================
def revenue_waterfall_report(conn, args):
    company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))
    where, params = ["1=1"], []
    where.append("c.company_id = ?")
    params.append(company_id)

    where_sql = " AND ".join(where)
    rows = conn.execute(f"""
        SELECT c.id as contract_id, c.customer_name, c.contract_number,
               c.total_value, c.allocated_value, c.contract_status,
               COUNT(po.id) as obligation_count,
               SUM(CASE WHEN po.obligation_status = 'satisfied' THEN 1 ELSE 0 END) as satisfied_count
        FROM advacct_revenue_contract c
        LEFT JOIN advacct_performance_obligation po ON po.contract_id = c.id
        WHERE {where_sql}
        GROUP BY c.id
        ORDER BY c.created_at DESC
    """, params).fetchall()

    ok({
        "report": "revenue_waterfall",
        "rows": [row_to_dict(r) for r in rows],
        "total_contracts": len(rows),
    })


# ===========================================================================
# 14. revenue-recognition-summary
# ===========================================================================
def revenue_recognition_summary(conn, args):
    sched_t = Table("advacct_revenue_schedule").as_("rs")
    company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))
    recognized_case = Case().when(sched_t.recognized == 1, sched_t.amount).else_("0")
    unrecognized_case = Case().when(sched_t.recognized == 0, sched_t.amount).else_("0")
    query = (
        Q.from_(sched_t)
        .select(
            sched_t.period_date,
            fn.Coalesce(DecimalSum(sched_t.amount), "0").as_("total_amount"),
            fn.Coalesce(DecimalSum(recognized_case), "0").as_("recognized_amount"),
            fn.Coalesce(DecimalSum(unrecognized_case), "0").as_("unrecognized_amount"),
            fn.Count("*").as_("entry_count"),
        )
        .groupby(sched_t.period_date)
        .orderby(sched_t.period_date)
    )
    params = []
    query = query.where(sched_t.company_id == P())
    params.append(company_id)
    rows = conn.execute(query.get_sql(), params).fetchall()

    out = []
    for item in rows:
        data = row_to_dict(item)
        for key in ("total_amount", "recognized_amount", "unrecognized_amount"):
            raw = data.get(key)
            if raw is None:
                raw = "0"
            data[key] = str(Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        out.append(data)

    ok({
        "report": "revenue_recognition_summary",
        "rows": out,
        "total_periods": len(out),
    })


# ===========================================================================
# 15. update-performance-obligation
# ===========================================================================
def update_performance_obligation(conn, args):
    """Update a performance obligation's pricing or method fields."""
    ob_id = getattr(args, "id", None)
    if not ob_id:
        err("--id is required")
    row = conn.execute("SELECT * FROM advacct_performance_obligation WHERE id = ?", (ob_id,)).fetchone()
    if not row:
        err(f"Performance obligation {ob_id} not found")
    contract_id = row_to_dict(row)["contract_id"]

    for arg_name in ("standalone_price", "allocated_price"):
        val = getattr(args, arg_name, None)
        if val is None:
            continue
        parsed = _money(val)
        if parsed is None or parsed < 0:
            err(f"Invalid {arg_name.replace('_', '-')}: {val}")

    data, changed = {}, []
    for arg_name, col_name in {
        "standalone_price": "standalone_price",
        "allocated_price": "allocated_price",
        "name": "name",
        "recognition_method": "recognition_method",
        "recognition_basis": "recognition_basis",
    }.items():
        val = getattr(args, arg_name, None)
        if val is not None:
            data[col_name] = val
            changed.append(col_name)

    if not data:
        err("No fields to update")

    current = row_to_dict(row)
    old_values = {col: current[col] for col in changed}
    data["updated_at"] = _now_iso()
    sql, params = dynamic_update("advacct_performance_obligation", data, where={"id": ob_id})
    conn.execute(sql, params)
    new_values = {col: data[col] for col in changed}

    # Keep the contract's allocated value the sum of its obligations'
    # allocations, as add-performance-obligation does, summed as Decimal.
    if "allocated_price" in data:
        prices = conn.execute(
            "SELECT allocated_price FROM advacct_performance_obligation WHERE contract_id = ?",
            (contract_id,)
        ).fetchall()
        total_allocated = sum((Decimal(p[0]) for p in prices), Decimal("0"))
        conn.execute(
            "UPDATE advacct_revenue_contract SET allocated_value = ?, updated_at = ? WHERE id = ?",
            (str(total_allocated.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
             data["updated_at"], contract_id)
        )

    audit(conn, SKILL, "update-performance-obligation", "advacct_performance_obligation", ob_id,
          old_values=old_values, new_values=new_values)
    conn.commit()
    ok({"id": ob_id, "updated_fields": changed})


# ===========================================================================
# 16. update-schedule-amounts
# ===========================================================================
def update_schedule_amounts(conn, args):
    """Re-spread the obligation's allocated price over its unrecognized periods.

    Reads the obligation's allocated price and writes one two-decimal amount
    per open period so the open periods plus what is already recognized sum
    back to the allocation, with any rounding remainder on the last period.
    Takes no --amount: set the allocation with update-performance-obligation
    --allocated-price first. Used for contract modifications (upgrade/downgrade)
    where only future (unrecognized) periods are re-priced.
    """
    ob_id = getattr(args, "obligation_id", None)
    if not ob_id:
        err("--obligation-id is required")
    row = conn.execute("SELECT * FROM advacct_performance_obligation WHERE id = ?", (ob_id,)).fetchone()
    if not row:
        err(f"Performance obligation {ob_id} not found")
    obligation = row_to_dict(row)

    if getattr(args, "amount", None) is not None:
        err("update-schedule-amounts takes no --amount: it re-spreads the obligation's "
            "allocated price over its unrecognized periods; set the allocation with "
            "update-performance-obligation --allocated-price")

    raw_allocated = obligation["allocated_price"]
    total = _money(raw_allocated)
    if total is None or total < 0:
        err(f"Performance obligation {ob_id} has an invalid allocated price: {raw_allocated}")
    alloc = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    recognized_rows = conn.execute(
        "SELECT id, amount FROM advacct_revenue_schedule WHERE obligation_id = ? AND recognized = 1",
        (ob_id,)).fetchall()
    recognized_total = Decimal("0")
    for rec in recognized_rows:
        rec_data = row_to_dict(rec)
        rec_amount = _money(rec_data["amount"])
        if rec_amount is None:
            err(f"Revenue schedule entry {rec_data['id']} holds an invalid amount: {rec_data['amount']}")
        recognized_total += rec_amount
    recognized_total = recognized_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if alloc < recognized_total:
        err(f"Allocated price {alloc} is below the {recognized_total} already recognized "
            f"for obligation {ob_id}")

    open_rows = conn.execute(
        "SELECT * FROM advacct_revenue_schedule WHERE obligation_id = ? AND recognized = 0 "
        "ORDER BY period_date, id",
        (ob_id,)).fetchall()
    open_entries = [row_to_dict(item) for item in open_rows]
    count = len(open_entries)
    if count == 0:
        ok({"obligation_id": ob_id, "allocated_price": str(alloc),
            "recognized_amount": str(recognized_total),
            "entries_updated": 0, "amounts": []})

    remaining = alloc - recognized_total
    per_period = (remaining / count).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    last_amount = remaining - per_period * (count - 1)
    new_amounts = [str(per_period)] * (count - 1) + [str(last_amount)]
    old_amounts = [entry["amount"] for entry in open_entries]

    spread_sql = update_row(
        "advacct_revenue_schedule",
        data={"amount": P()}, where={"id": P(), "recognized": 0})
    entries_updated = 0
    for entry, new_amount in zip(open_entries, new_amounts):
        cursor = conn.execute(spread_sql, (new_amount, entry["id"]))
        entries_updated += cursor.rowcount

    audit(conn, SKILL, "update-schedule-amounts", "advacct_revenue_schedule", ob_id,
          old_values={"amounts": old_amounts},
          new_values={"allocated_price": str(alloc), "recognized_amount": str(recognized_total),
                      "entries_updated": entries_updated, "amounts": new_amounts})
    conn.commit()
    ok({"obligation_id": ob_id, "allocated_price": str(alloc),
        "recognized_amount": str(recognized_total),
        "entries_updated": entries_updated, "amounts": new_amounts})


# ===========================================================================
# 17. recognize-schedule-entry
# ===========================================================================
def recognize_schedule_entry(conn, args):
    """Mark a single revenue schedule entry as recognized by its ID.

    Used for period-by-period recognition (e.g., from Stripe ASC 606 bridge)
    where only specific entries should be marked, not all at once.
    """
    entry_id = getattr(args, "id", None)
    if not entry_id:
        err("--id is required")
    row = conn.execute("SELECT * FROM advacct_revenue_schedule WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        err(f"Revenue schedule entry {entry_id} not found")

    data = row_to_dict(row)
    if data["recognized"] == 1:
        err(f"Revenue schedule entry {entry_id} is already recognized")

    accts = _recognition_accounts(conn, args, data["company_id"])
    try:
        gl_ids = _post_recognition(conn, data, accts)
    except (ValueError, NotImplementedError) as exc:
        conn.rollback()
        err(f"Revenue schedule entry {entry_id} was not recognized: "
            f"the ledger posting was refused: {exc}")

    audit(conn, SKILL, "recognize-schedule-entry", "advacct_revenue_schedule", entry_id,
          new_values={"recognized": 1, "gl_entry_ids": gl_ids})
    conn.commit()
    ok({"id": entry_id, "recognized": 1, "amount": data["amount"], "gl_entry_ids": gl_ids})


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------
ACTIONS = {
    "add-revenue-contract": add_revenue_contract,
    "update-revenue-contract": update_revenue_contract,
    "get-revenue-contract": get_revenue_contract,
    "list-revenue-contracts": list_revenue_contracts,
    "add-performance-obligation": add_performance_obligation,
    "list-performance-obligations": list_performance_obligations,
    "satisfy-performance-obligation": satisfy_performance_obligation,
    "update-performance-obligation": update_performance_obligation,
    "add-variable-consideration": add_variable_consideration,
    "list-variable-considerations": list_variable_considerations,
    "modify-contract": modify_contract,
    "calculate-revenue-schedule": calculate_revenue_schedule,
    "generate-revenue-entries": generate_revenue_entries,
    "update-schedule-amounts": update_schedule_amounts,
    "recognize-schedule-entry": recognize_schedule_entry,
    "revenue-waterfall-report": revenue_waterfall_report,
    "revenue-recognition-summary": revenue_recognition_summary,
}
