#!/usr/bin/env python3
"""ERPClaw AI Engine Skill -- db_query.py

AI-powered business analysis: anomaly detection, cash flow forecasting,
business rules, relationship scoring, conversation memory.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
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
    from erpclaw_lib.validation import check_input_lengths  # noqa: E402
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw-setup first: clawhub install erpclaw-setup", "suggestion": "clawhub install erpclaw-setup"}))
    sys.exit(1)

REQUIRED_TABLES = ["company", "account"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_ANOMALY_TYPES = {
    "price_spike", "volume_change", "duplicate_possible", "margin_erosion",
    "unusual_vendor", "pattern_break", "consumption_spike", "late_pattern",
    "round_number", "ghost_employee", "vendor_concentration",
    "sequence_violation", "benford_deviation", "budget_overrun",
    "inventory_shrinkage", "payment_pattern_shift",
}

VALID_SEVERITY = {"info", "warning", "critical"}

VALID_ANOMALY_STATUSES = {
    "new", "acknowledged", "investigated", "dismissed", "resolved",
}

VALID_SCENARIO_TYPES = {
    "price_change", "supplier_loss", "demand_shift", "cost_change",
    "hiring_impact", "expansion", "contraction",
}

VALID_FORECAST_SCENARIOS = {"pessimistic", "expected", "optimistic"}

VALID_RULE_ACTIONS = {"block", "warn", "notify", "auto_execute", "suggest"}

VALID_CONTEXT_TYPES = {
    "active_workflow", "pending_decision", "in_progress_analysis",
}

VALID_DECISION_STATUSES = {"pending", "decided", "expired"}

VALID_STRENGTH = {"weak", "moderate", "strong"}

VALID_CORRELATION_STATUSES = {"new", "validated", "dismissed"}

VALID_PARTY_TYPES = {"customer", "supplier"}

VALID_CREATED_BY = {"user", "ai"}

VALID_SOURCES = {"bank_feed", "ocr_vendor", "email_subject"}

STRENGTH_ORDER = {"weak": 1, "moderate": 2, "strong": 3}

# Severity mapping from plan flags to schema action column
SEVERITY_TO_ACTION = {
    "block": "block",
    "warn": "warn",
    "notify": "notify",
    "critical": "block",
    "warning": "warn",
    "info": "notify",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_json_arg(value, name):
    if not value:
        err(f"--{name} is required")
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        err(f"--{name} must be valid JSON")


def _validate_company(conn, company_id):
    """Validate company exists. Returns company row or calls _err."""
    if not company_id:
        err("--company-id is required")
    company = conn.execute(
        "SELECT * FROM company WHERE id = ?", (company_id,)
    ).fetchone()
    if not company:
        err(f"Company not found: {company_id}",
             suggestion="Run 'tutorial' to create a demo company, or 'setup company' to create your own.")
    return company


def _insert_anomaly(conn, anomaly_type, severity, entity_type, entity_id,
                    description, evidence, baseline=None, actual=None,
                    deviation_pct=None):
    """Insert an anomaly if not already exists (idempotent). Returns anomaly_id or None."""
    existing = conn.execute(
        """SELECT id FROM anomaly
           WHERE anomaly_type = ? AND entity_type = ? AND entity_id = ?
           AND status IN ('new', 'acknowledged')""",
        (anomaly_type, entity_type, entity_id),
    ).fetchone()
    if existing:
        return None

    anomaly_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO anomaly (id, anomaly_type, severity, entity_type,
           entity_id, description, evidence, baseline, actual,
           deviation_pct, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')""",
        (anomaly_id, anomaly_type, severity, entity_type, entity_id,
         description,
         json.dumps(evidence) if isinstance(evidence, dict) else evidence,
         json.dumps(baseline) if isinstance(baseline, dict) else baseline,
         json.dumps(actual) if isinstance(actual, dict) else actual,
         str(deviation_pct) if deviation_pct is not None else None),
    )
    return anomaly_id


# ============================================================================
# ACTION IMPLEMENTATIONS
# ============================================================================

# ---------------------------------------------------------------------------
# Anomaly Management (actions 3, 4)
# ---------------------------------------------------------------------------

def acknowledge_anomaly(conn, args):
    """Mark anomaly as acknowledged."""
    if not args.anomaly_id:
        err("--anomaly-id is required")

    anomaly = conn.execute(
        "SELECT * FROM anomaly WHERE id = ?", (args.anomaly_id,)
    ).fetchone()
    if not anomaly:
        err(f"Anomaly not found: {args.anomaly_id}")

    if anomaly["status"] not in ("new",):
        err(f"Cannot acknowledge anomaly in status: {anomaly['status']}")

    conn.execute(
        "UPDATE anomaly SET status = 'acknowledged' WHERE id = ?",
        (args.anomaly_id,),
    )
    conn.commit()
    audit(conn, "erpclaw-ai-engine", "acknowledge-anomaly", "anomaly", args.anomaly_id)
    conn.commit()

    updated = row_to_dict(conn.execute(
        "SELECT * FROM anomaly WHERE id = ?", (args.anomaly_id,)
    ).fetchone())
    ok({"anomaly": updated})


def dismiss_anomaly(conn, args):
    """Dismiss anomaly as false positive."""
    if not args.anomaly_id:
        err("--anomaly-id is required")

    anomaly = conn.execute(
        "SELECT * FROM anomaly WHERE id = ?", (args.anomaly_id,)
    ).fetchone()
    if not anomaly:
        err(f"Anomaly not found: {args.anomaly_id}")

    if anomaly["status"] in ("dismissed", "resolved"):
        err(f"Anomaly already in terminal status: {anomaly['status']}")

    conn.execute(
        """UPDATE anomaly SET status = 'dismissed', resolution_notes = ?
           WHERE id = ?""",
        (args.reason, args.anomaly_id),
    )
    conn.commit()
    audit(conn, "erpclaw-ai-engine", "dismiss-anomaly", "anomaly", args.anomaly_id)
    conn.commit()

    updated = row_to_dict(conn.execute(
        "SELECT * FROM anomaly WHERE id = ?", (args.anomaly_id,)
    ).fetchone())
    ok({"anomaly": updated})


# ---------------------------------------------------------------------------
# Scenario (actions 9, 10)
# ---------------------------------------------------------------------------

def create_scenario(conn, args):
    """Create a what-if scenario."""
    if not args.name:
        err("--name is required (scenario question)")
    if not args.company_id:
        err("--company-id is required")
    _validate_company(conn, args.company_id)

    scenario_type = args.scenario_type or "price_change"
    if scenario_type not in VALID_SCENARIO_TYPES:
        err(f"Invalid --scenario-type: {scenario_type}. "
             f"Must be one of: {', '.join(sorted(VALID_SCENARIO_TYPES))}")

    assumptions = {}
    if args.assumptions:
        assumptions = _parse_json_arg(args.assumptions, "assumptions")
    assumptions["company_id"] = args.company_id

    scenario_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO scenario (id, question, scenario_type, assumptions,
           created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (scenario_id, args.name, scenario_type,
         json.dumps(assumptions), _now()),
    )
    conn.commit()
    audit(conn, "erpclaw-ai-engine", "create-scenario", "scenario", scenario_id)
    conn.commit()

    row = row_to_dict(conn.execute(
        "SELECT * FROM scenario WHERE id = ?", (scenario_id,)
    ).fetchone())
    ok({"scenario": row})


def list_scenarios(conn, args):
    """List scenarios."""
    query = "SELECT * FROM scenario WHERE 1=1"
    params = []

    if args.company_id:
        query += " AND json_extract(assumptions, '$.company_id') = ?"
        params.append(args.company_id)

    count_query = query.replace("SELECT *", "SELECT COUNT(*) AS cnt", 1)
    total_count = conn.execute(count_query, params).fetchone()["cnt"]

    limit = int(args.limit)
    offset = int(args.offset)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = [row_to_dict(r) for r in conn.execute(query, params).fetchall()]
    ok({"scenarios": rows, "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# Business Rules (actions 11, 12, 13)
# ---------------------------------------------------------------------------

def add_business_rule(conn, args):
    """Create a natural language business rule."""
    if not args.rule_text:
        err("--rule-text is required")

    severity = args.severity or "warn"
    action_val = SEVERITY_TO_ACTION.get(severity, severity)
    if action_val not in VALID_RULE_ACTIONS:
        err(f"Invalid --severity: {severity}. "
             f"Must be one of: {', '.join(sorted(VALID_RULE_ACTIONS))}")

    parsed_condition = {}
    if args.name:
        parsed_condition["name"] = args.name
    if args.company_id:
        parsed_condition["company_id"] = args.company_id

    rule_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO business_rule (id, rule_text, parsed_condition,
           applies_to, action, active, times_triggered, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?)""",
        (rule_id, args.rule_text,
         json.dumps(parsed_condition) if parsed_condition else None,
         args.company_id, action_val, _now(), _now()),
    )
    conn.commit()
    audit(conn, "erpclaw-ai-engine", "add-business-rule", "business_rule", rule_id)
    conn.commit()

    row = row_to_dict(conn.execute(
        "SELECT * FROM business_rule WHERE id = ?", (rule_id,)
    ).fetchone())
    ok({"business_rule": row})


def list_business_rules(conn, args):
    """List business rules."""
    query = "SELECT * FROM business_rule WHERE 1=1"
    params = []

    if args.company_id:
        query += " AND (applies_to = ? OR applies_to IS NULL)"
        params.append(args.company_id)

    if args.is_active is not None and args.is_active != "":
        active_val = 1 if str(args.is_active).lower() in ("1", "true", "yes") else 0
        query += " AND active = ?"
        params.append(active_val)

    count_query = query.replace("SELECT *", "SELECT COUNT(*) AS cnt", 1)
    total_count = conn.execute(count_query, params).fetchone()["cnt"]

    limit = int(args.limit)
    offset = int(args.offset)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = [row_to_dict(r) for r in conn.execute(query, params).fetchall()]
    ok({"business_rules": rows, "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


def evaluate_business_rules(conn, args):
    """Evaluate business rules against a proposed action."""
    if not args.action_type:
        err("--action-type is required")
    if not args.action_data:
        err("--action-data is required (JSON)")

    action_data = _parse_json_arg(args.action_data, "action-data")

    # Load active rules
    query = "SELECT * FROM business_rule WHERE active = 1"
    params = []
    if args.company_id:
        query += " AND (applies_to = ? OR applies_to IS NULL)"
        params.append(args.company_id)

    rules = conn.execute(query, params).fetchall()

    triggered_rules = []
    for rule in rules:
        rule_dict = row_to_dict(rule)
        parsed = {}
        if rule_dict.get("parsed_condition"):
            try:
                parsed = json.loads(rule_dict["parsed_condition"])
            except (json.JSONDecodeError, TypeError):
                parsed = {}

        # Check if rule applies to this action type
        applies_to_action = parsed.get("applies_to_action")
        if applies_to_action and applies_to_action != args.action_type:
            continue

        # Check conditions
        conditions = parsed.get("conditions", [])
        if not conditions:
            # Rule with no conditions matches everything
            matched = True
        else:
            matched = _evaluate_conditions(conditions, action_data)

        if matched:
            # Increment trigger count
            conn.execute(
                """UPDATE business_rule SET times_triggered = times_triggered + 1,
                   last_triggered_at = ? WHERE id = ?""",
                (_now(), rule_dict["id"]),
            )
            triggered_rules.append({
                "rule_id": rule_dict["id"],
                "rule_text": rule_dict["rule_text"],
                "action": rule_dict["action"],
                "name": parsed.get("name"),
            })

    conn.commit()

    if triggered_rules:
        # Return the most restrictive action
        action_priority = {"block": 5, "warn": 4, "notify": 3,
                          "auto_execute": 2, "suggest": 1}
        triggered_rules.sort(
            key=lambda r: action_priority.get(r["action"], 0), reverse=True
        )
        ok({
            "triggered": True,
            "rules": triggered_rules,
            "recommended_action": triggered_rules[0]["action"],
        })
    else:
        ok({"triggered": False, "rules": [], "recommended_action": None})


def _evaluate_conditions(conditions, action_data):
    """Evaluate a list of conditions against action data."""
    for cond in conditions:
        field = cond.get("field")
        operator = cond.get("operator")
        value = cond.get("value")

        if not field or not operator:
            continue

        actual_value = action_data.get(field)
        if actual_value is None:
            return False

        try:
            if operator in (">", "<", ">=", "<="):
                actual_num = Decimal(str(actual_value))
                threshold = Decimal(str(value))
                if operator == ">" and not (actual_num > threshold):
                    return False
                elif operator == "<" and not (actual_num < threshold):
                    return False
                elif operator == ">=" and not (actual_num >= threshold):
                    return False
                elif operator == "<=" and not (actual_num <= threshold):
                    return False
            elif operator == "=":
                if str(actual_value) != str(value):
                    return False
            elif operator == "!=":
                if str(actual_value) == str(value):
                    return False
            elif operator == "contains":
                if str(value).lower() not in str(actual_value).lower():
                    return False
        except (ValueError, TypeError):
            return False

    return True


# ---------------------------------------------------------------------------
# Categorization (actions 14, 15)
# ---------------------------------------------------------------------------

def add_categorization_rule(conn, args):
    """Create an auto-categorization pattern."""
    if not args.pattern:
        err("--pattern is required")
    if not args.account_id:
        err("--account-id is required")

    # Validate account FK
    acct = conn.execute(
        "SELECT id FROM account WHERE id = ? OR name = ?",
        (args.account_id, args.account_id),
    ).fetchone()
    if not acct:
        err(f"Account not found: {args.account_id}")
    args.account_id = acct["id"]

    # Validate optional cost center FK
    if args.cost_center_id:
        cc = conn.execute(
            "SELECT id FROM cost_center WHERE id = ? OR name = ?",
            (args.cost_center_id, args.cost_center_id),
        ).fetchone()
        if not cc:
            err(f"Cost center not found: {args.cost_center_id}")
        args.cost_center_id = cc["id"]

    source = args.source or "bank_feed"
    if source not in VALID_SOURCES:
        err(f"Invalid --source: {source}. "
             f"Must be one of: {', '.join(sorted(VALID_SOURCES))}")

    rule_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO categorization_rule (id, pattern, source,
           target_account_id, target_cost_center_id, confidence,
           times_applied, times_overridden, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, '0.5', 0, 0, 'user', ?, ?)""",
        (rule_id, args.pattern, source, args.account_id,
         args.cost_center_id, _now(), _now()),
    )
    conn.commit()
    audit(conn, "erpclaw-ai-engine", "add-categorization-rule", "categorization_rule", rule_id)
    conn.commit()

    row = row_to_dict(conn.execute(
        "SELECT * FROM categorization_rule WHERE id = ?", (rule_id,)
    ).fetchone())
    ok({"categorization_rule": row})


def categorize_transaction(conn, args):
    """Auto-categorize a transaction using learned patterns."""
    if not args.description:
        err("--description is required")

    desc_lower = args.description.lower()

    # Load all rules ordered by confidence DESC
    rules = conn.execute(
        """SELECT * FROM categorization_rule
           ORDER BY confidence + 0 DESC, times_applied DESC"""
    ).fetchall()

    best_match = None
    for rule in rules:
        rule_dict = row_to_dict(rule)
        pattern = rule_dict["pattern"].lower()
        if pattern in desc_lower:
            best_match = rule_dict
            break

    if best_match:
        # Increment times_applied
        conn.execute(
            """UPDATE categorization_rule SET times_applied = times_applied + 1,
               last_applied_at = ? WHERE id = ?""",
            (_now(), best_match["id"]),
        )
        conn.commit()
        ok({
            "match": True,
            "rule_id": best_match["id"],
            "pattern": best_match["pattern"],
            "account_id": best_match["target_account_id"],
            "cost_center_id": best_match["target_cost_center_id"],
            "confidence": best_match["confidence"],
        })
    else:
        ok({"match": False, "rule_id": None, "account_id": None,
             "confidence": "0"})


# ---------------------------------------------------------------------------
# Conversation Context (actions 18, 19)
# ---------------------------------------------------------------------------

def save_conversation_context(conn, args):
    """Persist current conversation state."""
    if not args.context_data:
        err("--context-data is required (JSON)")

    data = _parse_json_arg(args.context_data, "context-data")

    context_type = data.get("context_type", "active_workflow")
    if context_type not in VALID_CONTEXT_TYPES:
        err(f"Invalid context_type: {context_type}. "
             f"Must be one of: {', '.join(sorted(VALID_CONTEXT_TYPES))}")

    ctx_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO conversation_context (id, user_id, context_type,
           summary, related_entities, state, last_active, priority)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ctx_id, data.get("user_id"), context_type,
         data.get("summary"), json.dumps(data.get("related_entities")),
         json.dumps(data.get("state")), _now(),
         data.get("priority", 0)),
    )
    conn.commit()

    row = row_to_dict(conn.execute(
        "SELECT * FROM conversation_context WHERE id = ?", (ctx_id,)
    ).fetchone())
    ok({"context": row})


def get_conversation_context(conn, args):
    """Resume from saved conversation context."""
    if args.context_id:
        row = conn.execute(
            "SELECT * FROM conversation_context WHERE id = ?",
            (args.context_id,),
        ).fetchone()
        if not row:
            err(f"Context not found: {args.context_id}")
    else:
        # Get latest active context
        row = conn.execute(
            """SELECT * FROM conversation_context
               ORDER BY last_active DESC LIMIT 1"""
        ).fetchone()
        if not row:
            ok({"context": None, "message": "No saved context found"})

    ctx = row_to_dict(row)

    # Also fetch any pending decisions for this context
    decisions = [row_to_dict(r) for r in conn.execute(
        """SELECT * FROM pending_decision
           WHERE context_id = ? AND status = 'pending'
           ORDER BY created_at DESC""",
        (ctx["id"],),
    ).fetchall()]

    ctx["pending_decisions"] = decisions
    ok({"context": ctx})


# ---------------------------------------------------------------------------
# Pending Decisions (action 20)
# ---------------------------------------------------------------------------

def add_pending_decision(conn, args):
    """Record a decision awaiting user input."""
    if not args.description:
        err("--description is required")

    options = None
    if args.options:
        options = _parse_json_arg(args.options, "options")

    # Use provided context_id or create a new context
    context_id = args.context_id
    if not context_id:
        context_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO conversation_context (id, context_type, summary,
               last_active, priority)
               VALUES (?, 'pending_decision', ?, ?, 0)""",
            (context_id, f"Decision: {args.description}", _now()),
        )

    decision_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO pending_decision (id, context_id, question, options,
           deadline, impact, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (decision_id, context_id, args.description,
         json.dumps(options) if options else None,
         args.to_date,  # reuse --to-date as deadline
         args.decision_type,  # reuse as impact description
         _now()),
    )
    conn.commit()

    row = row_to_dict(conn.execute(
        "SELECT * FROM pending_decision WHERE id = ?", (decision_id,)
    ).fetchone())
    ok({"pending_decision": row, "context_id": context_id})


# ---------------------------------------------------------------------------
# Audit (action 21)
# ---------------------------------------------------------------------------

def log_audit_conversation(conn, args):
    """Record AI action audit trail."""
    if not args.action_name:
        err("--action-name is required")

    details = None
    if args.details:
        details = _parse_json_arg(args.details, "details")

    audit_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO audit_conversation (id, timestamp, voucher_type,
           ai_interpretation, actions_taken)
           VALUES (?, ?, ?, ?, ?)""",
        (audit_id, _now(), args.action_name,
         args.result,
         json.dumps(details) if details else None),
    )
    conn.commit()

    row = row_to_dict(conn.execute(
        "SELECT * FROM audit_conversation WHERE id = ?", (audit_id,)
    ).fetchone())
    ok({"audit_entry": row})


# ---------------------------------------------------------------------------
# Relationship Scoring (actions 16, 17)
# ---------------------------------------------------------------------------

def score_relationship(conn, args):
    """Compute customer/supplier health score."""
    if not args.party_type:
        err("--party-type is required")
    if args.party_type not in VALID_PARTY_TYPES:
        err(f"Invalid --party-type: {args.party_type}. "
             f"Must be one of: {', '.join(sorted(VALID_PARTY_TYPES))}")
    if not args.party_id:
        err("--party-id is required")

    # Validate party exists
    if args.party_type == "customer":
        party = conn.execute(
            "SELECT id, company_id FROM customer WHERE id = ?",
            (args.party_id,)
        ).fetchone()
        if not party:
            err(f"Customer not found: {args.party_id}")
    else:
        party = conn.execute(
            "SELECT id, company_id FROM supplier WHERE id = ?",
            (args.party_id,)
        ).fetchone()
        if not party:
            err(f"Supplier not found: {args.party_id}")

    company_id = party["company_id"]
    today = _today()

    # --- Payment Score ---
    if args.party_type == "customer":
        invoices = conn.execute(
            """SELECT posting_date, due_date, grand_total, outstanding_amount
                FROM sales_invoice
                WHERE customer_id = ? AND status NOT IN ('draft', 'cancelled')""",
            (args.party_id,),
        ).fetchall()
    else:
        invoices = conn.execute(
            """SELECT posting_date, due_date, grand_total, outstanding_amount
                FROM purchase_invoice
                WHERE supplier_id = ? AND status NOT IN ('draft', 'cancelled')""",
            (args.party_id,),
        ).fetchall()

    payments = conn.execute(
        """SELECT posting_date, paid_amount FROM payment_entry
           WHERE party_type = ? AND party_id = ?
           AND status = 'submitted'""",
        (args.party_type, args.party_id),
    ).fetchall()

    total_invoices = len(invoices)
    if total_invoices == 0:
        # No history — return default scores
        score_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO relationship_score (id, party_type, party_id,
               score_date, overall_score, payment_score, volume_trend,
               profitability_score, risk_score, lifetime_value, factors,
               ai_summary, created_at)
               VALUES (?, ?, ?, ?, '50', '50', 'stable', '50', '50', '0',
                       ?, ?, ?)""",
            (score_id, args.party_type, args.party_id, today,
             json.dumps({"note": "No transaction history"}),
             "No transaction history available for scoring.",
             _now()),
        )
        conn.commit()
        row = row_to_dict(conn.execute(
            "SELECT * FROM relationship_score WHERE id = ?", (score_id,)
        ).fetchone())
        ok({"relationship_score": row})

    # Calculate payment score: on-time = 100, -2 per day late
    late_days_list = []
    for inv in invoices:
        if inv["due_date"] and inv["outstanding_amount"]:
            outstanding = to_decimal(str(inv["outstanding_amount"]))
            if outstanding > 0:
                days_overdue = (
                    datetime.strptime(today, "%Y-%m-%d")
                    - datetime.strptime(inv["due_date"], "%Y-%m-%d")
                ).days
                if days_overdue > 0:
                    late_days_list.append(days_overdue)

    if late_days_list:
        avg_late = sum(late_days_list) / len(late_days_list)
        payment_score = max(0, 100 - int(avg_late * 2))
    else:
        payment_score = 100

    # --- Volume Trend ---
    ninety_days_ago = (datetime.strptime(today, "%Y-%m-%d")
                       - timedelta(days=90)).strftime("%Y-%m-%d")
    one_eighty_days_ago = (datetime.strptime(today, "%Y-%m-%d")
                           - timedelta(days=180)).strftime("%Y-%m-%d")

    if args.party_type == "customer":
        recent_vol = to_decimal(str(conn.execute(
            """SELECT COALESCE(decimal_sum(grand_total), '0') as total
                FROM sales_invoice
                WHERE customer_id = ? AND posting_date >= ?
                AND status NOT IN ('draft', 'cancelled')""",
            (args.party_id, ninety_days_ago),
        ).fetchone()["total"]))
        prior_vol = to_decimal(str(conn.execute(
            """SELECT COALESCE(decimal_sum(grand_total), '0') as total
                FROM sales_invoice
                WHERE customer_id = ? AND posting_date >= ? AND posting_date < ?
                AND status NOT IN ('draft', 'cancelled')""",
            (args.party_id, one_eighty_days_ago, ninety_days_ago),
        ).fetchone()["total"]))
    else:
        recent_vol = to_decimal(str(conn.execute(
            """SELECT COALESCE(decimal_sum(grand_total), '0') as total
                FROM purchase_invoice
                WHERE supplier_id = ? AND posting_date >= ?
                AND status NOT IN ('draft', 'cancelled')""",
            (args.party_id, ninety_days_ago),
        ).fetchone()["total"]))
        prior_vol = to_decimal(str(conn.execute(
            """SELECT COALESCE(decimal_sum(grand_total), '0') as total
                FROM purchase_invoice
                WHERE supplier_id = ? AND posting_date >= ? AND posting_date < ?
                AND status NOT IN ('draft', 'cancelled')""",
            (args.party_id, one_eighty_days_ago, ninety_days_ago),
        ).fetchone()["total"]))

    if prior_vol > 0:
        vol_change = (recent_vol - prior_vol) / prior_vol
        if vol_change > Decimal("0.1"):
            volume_trend = "growing"
        elif vol_change < Decimal("-0.1"):
            volume_trend = "declining"
        else:
            volume_trend = "stable"
    else:
        volume_trend = "growing" if recent_vol > 0 else "stable"

    volume_score = 70  # default
    if volume_trend == "growing":
        volume_score = 90
    elif volume_trend == "declining":
        volume_score = 40

    # --- Profitability Score ---
    profitability_score = 70  # default — full COGS analysis would need more data

    # --- Risk Score ---
    overdue_count = len(late_days_list)
    if total_invoices > 0:
        overdue_pct = overdue_count / total_invoices
        risk_score = max(0, int(100 - overdue_pct * 100))
    else:
        risk_score = 50

    # --- Lifetime Value ---
    if args.party_type == "customer":
        lifetime_row = conn.execute(
            """SELECT COALESCE(decimal_sum(grand_total), '0') as total
                FROM sales_invoice
                WHERE customer_id = ? AND status NOT IN ('draft', 'cancelled')""",
            (args.party_id,),
        ).fetchone()
    else:
        lifetime_row = conn.execute(
            """SELECT COALESCE(decimal_sum(grand_total), '0') as total
                FROM purchase_invoice
                WHERE supplier_id = ? AND status NOT IN ('draft', 'cancelled')""",
            (args.party_id,),
        ).fetchone()
    lifetime_value = str(round_currency(to_decimal(str(lifetime_row["total"]))))

    # --- Overall Score (weighted average) ---
    overall = int(
        payment_score * 0.30
        + volume_score * 0.20
        + profitability_score * 0.25
        + risk_score * 0.25
    )

    # Build summary
    summary_parts = []
    if payment_score >= 80:
        summary_parts.append("consistent payment history")
    elif payment_score < 50:
        summary_parts.append("frequent late payments")
    if volume_trend == "growing":
        summary_parts.append("growing transaction volume")
    elif volume_trend == "declining":
        summary_parts.append("declining transaction volume")
    if risk_score >= 80:
        summary_parts.append("low risk profile")
    elif risk_score < 50:
        summary_parts.append("elevated risk due to overdue invoices")

    ai_summary = (f"{args.party_type.title()} relationship score: "
                  f"{overall}/100. "
                  + (", ".join(summary_parts) + "." if summary_parts else ""))

    factors = {
        "payment_score": payment_score,
        "volume_score": volume_score,
        "profitability_score": profitability_score,
        "risk_score": risk_score,
        "total_invoices": total_invoices,
        "overdue_invoices": overdue_count,
        "volume_trend": volume_trend,
    }

    score_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO relationship_score (id, party_type, party_id,
           score_date, overall_score, payment_score, volume_trend,
           profitability_score, risk_score, lifetime_value, factors,
           ai_summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (score_id, args.party_type, args.party_id, today,
         str(overall), str(payment_score), volume_trend,
         str(profitability_score), str(risk_score), lifetime_value,
         json.dumps(factors), ai_summary, _now()),
    )
    conn.commit()

    row = row_to_dict(conn.execute(
        "SELECT * FROM relationship_score WHERE id = ?", (score_id,)
    ).fetchone())
    ok({"relationship_score": row})


def list_relationship_scores(conn, args):
    """List relationship scores."""
    query = "SELECT * FROM relationship_score WHERE 1=1"
    params = []

    if args.party_type:
        query += " AND party_type = ?"
        params.append(args.party_type)

    if args.company_id:
        # Join through customer/supplier to filter by company
        query = """
            SELECT rs.* FROM relationship_score rs
            LEFT JOIN customer c ON rs.party_type = 'customer' AND rs.party_id = c.id
            LEFT JOIN supplier s ON rs.party_type = 'supplier' AND rs.party_id = s.id
            WHERE COALESCE(c.company_id, s.company_id) = ?
        """
        params = [args.company_id]
        if args.party_type:
            query += " AND rs.party_type = ?"
            params.append(args.party_type)

    count_query = query.replace("SELECT *", "SELECT COUNT(*) AS cnt", 1).replace("SELECT rs.*", "SELECT COUNT(*) AS cnt", 1)
    total_count = conn.execute(count_query, params).fetchone()["cnt"]

    limit = int(args.limit)
    offset = int(args.offset)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = [row_to_dict(r) for r in conn.execute(query, params).fetchall()]
    ok({"relationship_scores": rows, "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# Anomaly Detection (actions 1, 2)
# ---------------------------------------------------------------------------

def detect_anomalies(conn, args):
    """Run anomaly detection across all modules."""
    if not args.company_id:
        err("--company-id is required")
    _validate_company(conn, args.company_id)

    from_date = args.from_date or "2000-01-01"
    to_date = args.to_date or _today()
    company_id = args.company_id

    new_anomalies = []
    by_type = {}
    by_severity = {}

    # --- Heuristic 1: duplicate_possible ---
    dupes = conn.execute(
        """SELECT g1.id as id1, g2.id as id2,
                  g1.posting_date as date1, g2.posting_date as date2,
                  g1.account_id, g1.debit, g1.credit
           FROM gl_entry g1
           JOIN gl_entry g2 ON g1.account_id = g2.account_id
             AND g1.debit = g2.debit AND g1.credit = g2.credit
             AND g1.id < g2.id
             AND ABS(julianday(g2.posting_date) - julianday(g1.posting_date)) <= 7
           WHERE g1.account_id IN (SELECT id FROM account WHERE company_id = ?)
             AND g1.posting_date >= ? AND g1.posting_date <= ?
             AND g1.is_cancelled = 0 AND g2.is_cancelled = 0""",
        (company_id, from_date, to_date),
    ).fetchall()

    for dupe in dupes:
        d = row_to_dict(dupe)
        amount = d["debit"] if d["debit"] and d["debit"] != "0" else d["credit"]
        aid = _insert_anomaly(
            conn, "duplicate_possible", "warning",
            "gl_entry", d["id1"],
            f"Possible duplicate GL entry: same account and amount ${amount} "
            f"within 7 days ({d['date1']} and {d['date2']})",
            {"company_id": company_id, "duplicate_id": d["id2"],
             "amount": str(amount), "account_id": d["account_id"]},
        )
        if aid:
            new_anomalies.append(aid)
            by_type["duplicate_possible"] = by_type.get("duplicate_possible", 0) + 1
            by_severity["warning"] = by_severity.get("warning", 0) + 1

    # --- Heuristic 2: round_number ---
    rounds = conn.execute(
        """SELECT id, posting_date, account_id, debit, credit
           FROM gl_entry
           WHERE account_id IN (SELECT id FROM account WHERE company_id = ?)
             AND posting_date >= ? AND posting_date <= ?
             AND is_cancelled = 0
             AND (
               (debit + 0 >= 1000 AND (debit + 0) % 1000 = 0
                AND debit != '0')
               OR
               (credit + 0 >= 1000 AND (credit + 0) % 1000 = 0
                AND credit != '0')
             )""",
        (company_id, from_date, to_date),
    ).fetchall()

    for entry in rounds:
        e = row_to_dict(entry)
        amount = e["debit"] if e["debit"] and e["debit"] != "0" else e["credit"]
        aid = _insert_anomaly(
            conn, "round_number", "info",
            "gl_entry", e["id"],
            f"Suspiciously round GL entry: ${amount} on {e['posting_date']}",
            {"company_id": company_id, "amount": str(amount),
             "account_id": e["account_id"]},
        )
        if aid:
            new_anomalies.append(aid)
            by_type["round_number"] = by_type.get("round_number", 0) + 1
            by_severity["info"] = by_severity.get("info", 0) + 1

    # --- Heuristic 3: budget_overrun ---
    budgets = conn.execute(
        """SELECT id, account_id, cost_center_id, budget_amount
           FROM budget
           WHERE company_id = ?""",
        (company_id,),
    ).fetchall()

    for b in budgets:
        bd = row_to_dict(b)
        budget_amt = to_decimal(str(bd["budget_amount"])) if bd["budget_amount"] else Decimal("0")

        if not bd["account_id"]:
            continue

        # Get actual spend
        actual_query = """
            SELECT COALESCE(decimal_sum(debit), '0') - COALESCE(decimal_sum(credit), '0') as actual
            FROM gl_entry
            WHERE account_id = ? AND posting_date >= ? AND posting_date <= ?
            AND is_cancelled = 0
        """
        actual_params = [bd["account_id"], from_date, to_date]
        if bd["cost_center_id"]:
            actual_query += " AND cost_center_id = ?"
            actual_params.append(bd["cost_center_id"])

        actual = to_decimal(str(conn.execute(actual_query, actual_params).fetchone()["actual"]))

        if budget_amt > 0 and actual > budget_amt:
            deviation = ((actual - budget_amt) / budget_amt * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            severity = "critical" if deviation > Decimal("10") else "warning"
            aid = _insert_anomaly(
                conn, "budget_overrun", severity,
                "budget", bd["id"],
                f"Budget overrun: actual ${actual} exceeds "
                f"budget ${budget_amt} ({deviation}% over)",
                {"company_id": company_id, "budget_id": bd["id"],
                 "account_id": bd["account_id"],
                 "cost_center_id": bd["cost_center_id"]},
                baseline={"budget_amount": str(budget_amt)},
                actual={"actual_spend": str(round_currency(actual))},
                deviation_pct=str(deviation),
            )
            if aid:
                new_anomalies.append(aid)
                by_type["budget_overrun"] = by_type.get("budget_overrun", 0) + 1
                by_severity[severity] = by_severity.get(severity, 0) + 1

    # --- Heuristic 4: late_pattern ---
    overdue = conn.execute(
        """SELECT id, customer_id, posting_date, due_date,
                  outstanding_amount, grand_total
           FROM sales_invoice
           WHERE company_id = ?
             AND status IN ('submitted', 'partially_paid', 'overdue')
             AND due_date < ?
             AND outstanding_amount + 0 > 0""",
        (company_id, to_date),
    ).fetchall()

    for inv in overdue:
        i = row_to_dict(inv)
        days_late = (
            datetime.strptime(to_date, "%Y-%m-%d")
            - datetime.strptime(i["due_date"], "%Y-%m-%d")
        ).days
        severity = "critical" if days_late >= 30 else "warning"
        aid = _insert_anomaly(
            conn, "late_pattern", severity,
            "sales_invoice", i["id"],
            f"Invoice {days_late} days past due. "
            f"Outstanding: ${i['outstanding_amount']} of ${i['grand_total']}",
            {"company_id": company_id, "customer_id": i["customer_id"],
             "days_late": days_late},
            deviation_pct=days_late,
        )
        if aid:
            new_anomalies.append(aid)
            by_type["late_pattern"] = by_type.get("late_pattern", 0) + 1
            by_severity[severity] = by_severity.get(severity, 0) + 1

    # --- Heuristic 5: volume_change ---
    period_days = (
        datetime.strptime(to_date, "%Y-%m-%d")
        - datetime.strptime(from_date, "%Y-%m-%d")
    ).days
    if period_days > 0:
        prior_from = (datetime.strptime(from_date, "%Y-%m-%d")
                      - timedelta(days=period_days)).strftime("%Y-%m-%d")
        prior_to = from_date

        _vol_queries = [
            ("SELECT COUNT(*) as cnt, COALESCE(decimal_sum(grand_total), '0') as total "
             "FROM sales_invoice WHERE company_id = ? AND posting_date >= ? AND posting_date <= ? "
             "AND status NOT IN ('draft', 'cancelled')",
             "SELECT COUNT(*) as cnt, COALESCE(decimal_sum(grand_total), '0') as total "
             "FROM sales_invoice WHERE company_id = ? AND posting_date >= ? AND posting_date < ? "
             "AND status NOT IN ('draft', 'cancelled')",
             "sales"),
            ("SELECT COUNT(*) as cnt, COALESCE(decimal_sum(grand_total), '0') as total "
             "FROM purchase_invoice WHERE company_id = ? AND posting_date >= ? AND posting_date <= ? "
             "AND status NOT IN ('draft', 'cancelled')",
             "SELECT COUNT(*) as cnt, COALESCE(decimal_sum(grand_total), '0') as total "
             "FROM purchase_invoice WHERE company_id = ? AND posting_date >= ? AND posting_date < ? "
             "AND status NOT IN ('draft', 'cancelled')",
             "purchases"),
        ]
        for cur_sql, pri_sql, label in _vol_queries:
            current = conn.execute(cur_sql, (company_id, from_date, to_date)).fetchone()
            prior = conn.execute(pri_sql, (company_id, prior_from, prior_to)).fetchone()

            cur_total = to_decimal(str(current["total"]))
            pri_total = to_decimal(str(prior["total"]))
            if pri_total > 0:
                pct_change = ((cur_total - pri_total)
                              / pri_total * Decimal("100"))
                if abs(pct_change) > Decimal("30"):
                    severity = "critical" if abs(pct_change) > Decimal("50") else "warning"
                    direction = "increased" if pct_change > 0 else "decreased"
                    pct_rounded = pct_change.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                    aid = _insert_anomaly(
                        conn, "volume_change", severity,
                        "company", f"{company_id}:{label}",
                        f"{label.title()} volume {direction} by "
                        f"{abs(pct_rounded)}% vs prior period",
                        {"company_id": company_id, "module": label,
                         "current_total": str(round_currency(cur_total)),
                         "prior_total": str(round_currency(pri_total))},
                        baseline={"prior_total": str(round_currency(pri_total)),
                                  "prior_count": prior["cnt"]},
                        actual={"current_total": str(round_currency(cur_total)),
                                "current_count": current["cnt"]},
                        deviation_pct=str(pct_rounded),
                    )
                    if aid:
                        new_anomalies.append(aid)
                        by_type["volume_change"] = by_type.get("volume_change", 0) + 1
                        by_severity[severity] = by_severity.get(severity, 0) + 1

    conn.commit()
    ok({
        "anomalies_detected": len(new_anomalies),
        "anomaly_ids": new_anomalies,
        "by_type": by_type,
        "by_severity": by_severity,
    })


def list_anomalies(conn, args):
    """Query detected anomalies."""
    query = "SELECT * FROM anomaly WHERE 1=1"
    params = []

    if args.company_id:
        query += " AND json_extract(evidence, '$.company_id') = ?"
        params.append(args.company_id)

    if args.severity:
        if args.severity not in VALID_SEVERITY:
            err(f"Invalid --severity: {args.severity}")
        query += " AND severity = ?"
        params.append(args.severity)

    if args.status:
        if args.status not in VALID_ANOMALY_STATUSES:
            err(f"Invalid --status: {args.status}")
        query += " AND status = ?"
        params.append(args.status)

    count_query = query.replace("SELECT *", "SELECT COUNT(*) AS cnt", 1)
    total_count = conn.execute(count_query, params).fetchone()["cnt"]

    limit = int(args.limit)
    offset = int(args.offset)
    query += " ORDER BY detected_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = [row_to_dict(r) for r in conn.execute(query, params).fetchall()]
    ok({"anomalies": rows, "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# Cash Flow Forecasting (actions 5, 6)
# ---------------------------------------------------------------------------

def forecast_cash_flow(conn, args):
    """Generate cash flow forecast with 3 scenarios."""
    if not args.company_id:
        err("--company-id is required")
    _validate_company(conn, args.company_id)

    company_id = args.company_id
    horizon = int(args.horizon_days) if args.horizon_days else 30
    today = _today()
    horizon_date = (datetime.strptime(today, "%Y-%m-%d")
                    + timedelta(days=horizon)).strftime("%Y-%m-%d")

    # Starting balance: sum of cash/bank accounts
    bal_row = conn.execute(
        """SELECT COALESCE(decimal_sum(debit), '0') as total_debit,
                  COALESCE(decimal_sum(credit), '0') as total_credit
           FROM gl_entry g
           JOIN account a ON g.account_id = a.id
           WHERE a.company_id = ? AND a.account_type IN ('bank', 'cash')
           AND g.is_cancelled = 0""",
        (company_id,),
    ).fetchone()
    starting_balance = round_currency(to_decimal(str(bal_row["total_debit"])) - to_decimal(str(bal_row["total_credit"])))

    # Projected inflows: open AR
    ar_rows = conn.execute(
        """SELECT due_date, outstanding_amount FROM sales_invoice
           WHERE company_id = ?
           AND status IN ('submitted', 'partially_paid', 'overdue')
           AND outstanding_amount + 0 > 0""",
        (company_id,),
    ).fetchall()

    inflows = []
    total_inflows = Decimal("0")
    for ar in ar_rows:
        amt = to_decimal(str(ar["outstanding_amount"]))
        due = ar["due_date"] or today
        inflows.append({"date": due, "amount": str(amt)})
        total_inflows += amt

    # Projected outflows: open AP
    ap_rows = conn.execute(
        """SELECT due_date, outstanding_amount FROM purchase_invoice
           WHERE company_id = ?
           AND status IN ('submitted', 'partially_paid', 'overdue')
           AND outstanding_amount + 0 > 0""",
        (company_id,),
    ).fetchall()

    outflows = []
    total_outflows = Decimal("0")
    for ap in ap_rows:
        amt = to_decimal(str(ap["outstanding_amount"]))
        due = ap["due_date"] or today
        outflows.append({"date": due, "amount": str(amt)})
        total_outflows += amt

    # Generate 3 scenarios
    scenarios_data = {
        "pessimistic": {"inflow_mult": Decimal("0.7"),
                        "outflow_mult": Decimal("1.2")},
        "expected": {"inflow_mult": Decimal("0.9"),
                     "outflow_mult": Decimal("1.0")},
        "optimistic": {"inflow_mult": Decimal("1.0"),
                       "outflow_mult": Decimal("0.8")},
    }

    forecast_ids = []
    balances = {}
    start_bal = to_decimal(str(starting_balance))

    for scenario_name, mults in scenarios_data.items():
        adj_inflows = total_inflows * mults["inflow_mult"]
        adj_outflows = total_outflows * mults["outflow_mult"]
        projected = start_bal + adj_inflows - adj_outflows

        forecast_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO cash_flow_forecast (id, forecast_date, generated_at,
               horizon_days, starting_balance, projected_inflows,
               projected_outflows, projected_balance,
               confidence_interval, assumptions, scenario)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (forecast_id, today, _now(), horizon,
             str(start_bal),
             json.dumps(inflows),
             json.dumps(outflows),
             str(round_currency(projected)),
             None,  # filled after all scenarios
             json.dumps({"company_id": company_id,
                         "inflow_multiplier": str(mults["inflow_mult"]),
                         "outflow_multiplier": str(mults["outflow_mult"])}),
             scenario_name),
        )
        forecast_ids.append(forecast_id)
        balances[scenario_name] = round_currency(projected)

    # Update confidence intervals on all forecasts
    ci = {
        "low": str(balances["pessimistic"]),
        "mid": str(balances["expected"]),
        "high": str(balances["optimistic"]),
    }
    for fid in forecast_ids:
        conn.execute(
            "UPDATE cash_flow_forecast SET confidence_interval = ? WHERE id = ?",
            (json.dumps(ci), fid),
        )

    conn.commit()

    ok({
        "forecast_ids": forecast_ids,
        "starting_balance": str(start_bal),
        "horizon_days": horizon,
        "scenarios": {
            name: str(bal) for name, bal in balances.items()
        },
        "confidence_interval": ci,
        "total_ar": str(round_currency(total_inflows)),
        "total_ap": str(round_currency(total_outflows)),
    })


def get_forecast(conn, args):
    """Retrieve latest forecast."""
    query = """SELECT * FROM cash_flow_forecast WHERE 1=1"""
    params = []

    if args.company_id:
        query += " AND json_extract(assumptions, '$.company_id') = ?"
        params.append(args.company_id)

    query += " ORDER BY generated_at DESC LIMIT 3"

    rows = [row_to_dict(r) for r in conn.execute(query, params).fetchall()]
    if not rows:
        ok({"forecasts": [], "message": "No forecasts found"})

    ok({"forecasts": rows, "count": len(rows)})


# ---------------------------------------------------------------------------
# Correlation Discovery (actions 7, 8)
# ---------------------------------------------------------------------------

def discover_correlations(conn, args):
    """Find cross-module patterns."""
    if not args.company_id:
        err("--company-id is required")
    _validate_company(conn, args.company_id)

    company_id = args.company_id
    from_date = args.from_date or "2000-01-01"
    to_date = args.to_date or _today()

    new_correlations = []

    # Correlation 1: Sales vs Purchase volume
    sales = conn.execute(
        """SELECT COALESCE(decimal_sum(grand_total), '0') as total,
                  COUNT(*) as cnt
           FROM sales_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date <= ?
           AND status NOT IN ('draft', 'cancelled')""",
        (company_id, from_date, to_date),
    ).fetchone()

    purchases = conn.execute(
        """SELECT COALESCE(decimal_sum(grand_total), '0') as total,
                  COUNT(*) as cnt
           FROM purchase_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date <= ?
           AND status NOT IN ('draft', 'cancelled')""",
        (company_id, from_date, to_date),
    ).fetchone()

    sales_total = to_decimal(str(sales["total"]))
    purchases_total = to_decimal(str(purchases["total"]))
    if sales["cnt"] > 0 and purchases["cnt"] > 0:
        # Check if both are positive (same direction)
        ratio = (purchases_total / sales_total).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP) if sales_total > 0 else Decimal("0")
        if Decimal("0.3") < ratio < Decimal("3.0"):
            strength = "strong" if Decimal("0.5") < ratio < Decimal("2.0") else "moderate"
        else:
            strength = "weak"

        corr_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO correlation (id, discovered_at, module_a, module_b,
               description, evidence, strength, statistical_confidence,
               actionable, suggested_action, status)
               VALUES (?, ?, 'selling', 'buying', ?, ?, ?, ?, ?, ?, 'new')""",
            (corr_id, _now(),
             f"Sales-to-purchase ratio of {ratio} detected. "
             f"Sales: ${round_currency(sales_total)}, Purchases: ${round_currency(purchases_total)}",
             json.dumps({"company_id": company_id,
                         "sales_total": str(round_currency(sales_total)),
                         "purchases_total": str(round_currency(purchases_total)),
                         "ratio": str(ratio)}),
             strength,
             f"{min(sales['cnt'], purchases['cnt']) * 10}",
             1 if strength in ("strong", "moderate") else 0,
             "Review procurement efficiency relative to sales volume"
             if ratio > Decimal("0.7") else None),
        )
        new_correlations.append(corr_id)

    # Correlation 2: Payment timing pattern
    pay_data = conn.execute(
        """SELECT pe.party_type, COUNT(*) as cnt,
                  AVG(julianday(pe.posting_date) - julianday(
                    CASE WHEN pe.party_type = 'customer'
                         THEN (SELECT si.posting_date FROM sales_invoice si
                               WHERE si.customer_id = pe.party_id LIMIT 1)
                         ELSE (SELECT pi.posting_date FROM purchase_invoice pi
                               WHERE pi.supplier_id = pe.party_id LIMIT 1)
                    END
                  )) as avg_days
           FROM payment_entry pe
           WHERE pe.company_id = ? AND pe.posting_date >= ? AND pe.posting_date <= ?
           AND pe.status = 'submitted'
           GROUP BY pe.party_type""",
        (company_id, from_date, to_date),
    ).fetchall()

    if len(pay_data) >= 1:
        for pd in pay_data:
            pdd = row_to_dict(pd)
            if pdd["avg_days"] is not None:
                avg_d = round(pdd["avg_days"], 1)
                strength = "strong" if abs(avg_d) < 15 else "moderate" if abs(avg_d) < 30 else "weak"
                corr_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO correlation (id, discovered_at, module_a, module_b,
                       description, evidence, strength, actionable, status)
                       VALUES (?, ?, 'payments', ?, ?, ?, ?, 0, 'new')""",
                    (corr_id, _now(),
                     pdd["party_type"],
                     f"Average {pdd['party_type']} payment timing: "
                     f"{avg_d} days from invoice",
                     json.dumps({"company_id": company_id,
                                 "party_type": pdd["party_type"],
                                 "avg_payment_days": avg_d,
                                 "payment_count": pdd["cnt"]}),
                     strength),
                )
                new_correlations.append(corr_id)

    conn.commit()
    ok({
        "correlations_discovered": len(new_correlations),
        "correlation_ids": new_correlations,
    })


def list_correlations(conn, args):
    """List discovered correlations."""
    query = "SELECT * FROM correlation WHERE 1=1"
    params = []

    if args.company_id:
        query += " AND json_extract(evidence, '$.company_id') = ?"
        params.append(args.company_id)

    if args.min_strength:
        min_val = STRENGTH_ORDER.get(args.min_strength, 0)
        strengths = [s for s, v in STRENGTH_ORDER.items() if v >= min_val]
        if strengths:
            placeholders = ",".join(["?" for _ in strengths])
            query += f" AND strength IN ({placeholders})"
            params.extend(strengths)

    count_query = query.replace("SELECT *", "SELECT COUNT(*) AS cnt", 1)
    total_count = conn.execute(count_query, params).fetchone()["cnt"]

    limit = int(args.limit)
    offset = int(args.offset)
    query += " ORDER BY discovered_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = [row_to_dict(r) for r in conn.execute(query, params).fetchall()]
    ok({"correlations": rows, "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# Status (action 22)
# ---------------------------------------------------------------------------

def status_action(conn, args):
    """AI engine summary."""
    result = {}

    # Anomaly counts
    anomaly_q = "SELECT severity, COUNT(*) as cnt FROM anomaly WHERE status = 'new'"
    anomaly_params = []
    if args.company_id:
        anomaly_q += " AND json_extract(evidence, '$.company_id') = ?"
        anomaly_params.append(args.company_id)
    anomaly_q += " GROUP BY severity"

    anomaly_counts = {}
    for row in conn.execute(anomaly_q, anomaly_params).fetchall():
        anomaly_counts[row["severity"]] = row["cnt"]
    result["anomalies"] = {
        "new_total": sum(anomaly_counts.values()),
        "by_severity": anomaly_counts,
    }

    # Forecast
    forecast_q = "SELECT COUNT(*) as cnt FROM cash_flow_forecast"
    forecast_params = []
    if args.company_id:
        forecast_q += " WHERE json_extract(assumptions, '$.company_id') = ?"
        forecast_params.append(args.company_id)
    result["forecasts"] = conn.execute(forecast_q, forecast_params).fetchone()["cnt"]

    # Rules
    result["business_rules"] = {
        "active": conn.execute(
            "SELECT COUNT(*) as cnt FROM business_rule WHERE active = 1"
        ).fetchone()["cnt"],
        "total": conn.execute(
            "SELECT COUNT(*) as cnt FROM business_rule"
        ).fetchone()["cnt"],
    }

    # Categorization rules
    result["categorization_rules"] = conn.execute(
        "SELECT COUNT(*) as cnt FROM categorization_rule"
    ).fetchone()["cnt"]

    # Correlations
    corr_q = "SELECT COUNT(*) as cnt FROM correlation WHERE status = 'new'"
    corr_params = []
    if args.company_id:
        corr_q += " AND json_extract(evidence, '$.company_id') = ?"
        corr_params.append(args.company_id)
    result["correlations"] = conn.execute(corr_q, corr_params).fetchone()["cnt"]

    # Scenarios
    scen_q = "SELECT COUNT(*) as cnt FROM scenario"
    scen_params = []
    if args.company_id:
        scen_q += " WHERE json_extract(assumptions, '$.company_id') = ?"
        scen_params.append(args.company_id)
    result["scenarios"] = conn.execute(scen_q, scen_params).fetchone()["cnt"]

    # Relationship scores
    result["relationship_scores"] = conn.execute(
        "SELECT COUNT(*) as cnt FROM relationship_score"
    ).fetchone()["cnt"]

    # Pending decisions
    result["pending_decisions"] = conn.execute(
        "SELECT COUNT(*) as cnt FROM pending_decision WHERE status = 'pending'"
    ).fetchone()["cnt"]

    # Conversation contexts
    result["active_contexts"] = conn.execute(
        "SELECT COUNT(*) as cnt FROM conversation_context"
    ).fetchone()["cnt"]

    # Audit log entries
    result["audit_entries"] = conn.execute(
        "SELECT COUNT(*) as cnt FROM audit_conversation"
    ).fetchone()["cnt"]

    ok(result)


# ============================================================================
# ACTION REGISTRY
# ============================================================================

ACTIONS = {
    "detect-anomalies": detect_anomalies,
    "list-anomalies": list_anomalies,
    "acknowledge-anomaly": acknowledge_anomaly,
    "dismiss-anomaly": dismiss_anomaly,
    "forecast-cash-flow": forecast_cash_flow,
    "get-forecast": get_forecast,
    "discover-correlations": discover_correlations,
    "list-correlations": list_correlations,
    "create-scenario": create_scenario,
    "list-scenarios": list_scenarios,
    "add-business-rule": add_business_rule,
    "list-business-rules": list_business_rules,
    "evaluate-business-rules": evaluate_business_rules,
    "add-categorization-rule": add_categorization_rule,
    "categorize-transaction": categorize_transaction,
    "score-relationship": score_relationship,
    "list-relationship-scores": list_relationship_scores,
    "save-conversation-context": save_conversation_context,
    "get-conversation-context": get_conversation_context,
    "add-pending-decision": add_pending_decision,
    "log-audit-conversation": log_audit_conversation,
    "status": status_action,
}


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="ERPClaw AI Engine")
    parser.add_argument("--action", required=True, choices=list(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Entity IDs
    parser.add_argument("--anomaly-id")
    parser.add_argument("--context-id")
    parser.add_argument("--company-id")

    # Detection / filter
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--severity")
    parser.add_argument("--status")

    # Forecast
    parser.add_argument("--horizon-days")

    # Scenario
    parser.add_argument("--scenario-type")
    parser.add_argument("--assumptions")
    parser.add_argument("--name")

    # Business rules
    parser.add_argument("--rule-text")
    parser.add_argument("--is-active")
    parser.add_argument("--action-type")
    parser.add_argument("--action-data")

    # Categorization
    parser.add_argument("--pattern")
    parser.add_argument("--account-id")
    parser.add_argument("--description")
    parser.add_argument("--amount")
    parser.add_argument("--source")
    parser.add_argument("--cost-center-id")

    # Relationship
    parser.add_argument("--party-type")
    parser.add_argument("--party-id")

    # Context / Decision
    parser.add_argument("--context-data")
    parser.add_argument("--decision-type")
    parser.add_argument("--options")

    # Audit
    parser.add_argument("--action-name")
    parser.add_argument("--details")
    parser.add_argument("--result")

    # Correlation
    parser.add_argument("--min-strength")

    # General
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")
    parser.add_argument("--reason")

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
        sys.stderr.write(f"[erpclaw-ai-engine] {e}\n")
        err("An unexpected error occurred")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
