"""ERPClaw Advanced Accounting -- Cross-domain reports and status

Aggregated reports and the skill status action.
Imported by db_query.py (unified router).
"""
import os
import sys
import json
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext

try:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.query_helpers import resolve_company_id, resolve_scope_company
    from erpclaw_lib.query import P, Q, Table
except ImportError:
    pass

SKILL = "erpclaw-accounting-adv"
VERSION = "1.0.0"

ALL_TABLES = [
    "advacct_revenue_contract", "advacct_performance_obligation",
    "advacct_variable_consideration", "advacct_revenue_schedule",
    "advacct_lease", "advacct_lease_payment", "advacct_amortization_entry",
    "advacct_ic_transaction", "advacct_transfer_price_rule",
    "advacct_consolidation_group", "advacct_group_entity", "advacct_elimination_entry",
]


# ===========================================================================
# 1. standards-compliance-dashboard
# ===========================================================================
def standards_compliance_dashboard(conn, args):
    company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))
    where, params = ["1=1"], []
    where.append("company_id = ?")
    params.append(company_id)
    where_sql = " AND ".join(where)

    # Revenue (ASC 606)
    revenue_contracts = conn.execute(
        f"SELECT COUNT(*) FROM advacct_revenue_contract WHERE {where_sql}", params
    ).fetchone()[0]
    unsatisfied_obligations = conn.execute(
        f"SELECT COUNT(*) FROM advacct_performance_obligation WHERE obligation_status != 'satisfied' AND {where_sql}", params
    ).fetchone()[0]

    # Leases (ASC 842)
    active_leases = conn.execute(
        f"SELECT COUNT(*) FROM advacct_lease WHERE lease_status = 'active' AND {where_sql}", params
    ).fetchone()[0]
    leases_without_rou = conn.execute(
        f"SELECT COUNT(*) FROM advacct_lease WHERE rou_asset_value IS NULL AND lease_status != 'draft' AND {where_sql}", params
    ).fetchone()[0]

    # Intercompany
    unposted_ic = conn.execute(
        f"SELECT COUNT(*) FROM advacct_ic_transaction WHERE ic_status NOT IN ('posted','eliminated') AND {where_sql}", params
    ).fetchone()[0]

    # Consolidation
    active_groups = conn.execute(
        f"SELECT COUNT(*) FROM advacct_consolidation_group WHERE group_status = 'active' AND {where_sql}", params
    ).fetchone()[0]

    ok({
        "report": "standards_compliance_dashboard",
        "asc_606": {
            "revenue_contracts": revenue_contracts,
            "unsatisfied_obligations": unsatisfied_obligations,
        },
        "asc_842": {
            "active_leases": active_leases,
            "leases_without_rou_calculation": leases_without_rou,
        },
        "intercompany": {
            "unposted_transactions": unposted_ic,
        },
        "consolidation": {
            "active_groups": active_groups,
        },
    })


# ===========================================================================
# 2. status
# ===========================================================================
def status_action(conn, args):
    counts = {}
    for tbl in ALL_TABLES:
        try:
            counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception:
            counts[tbl] = 0

    ok({
        "skill": SKILL,
        "version": VERSION,
        "total_tables": len(ALL_TABLES),
        "record_counts": counts,
    })


def _benefit_money(value, label, signed=False):
    pattern = r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,2})?" if signed else r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,2})?"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        err(f"{label} must be a decimal amount with at most two decimal places")
    return Decimal(value)


def calculate_benefit_liability(conn, args):
    """Preview employer liability and an operator-supplied annual expense schedule.

    Deferral amounts and expense are already employer-specific. This calculation
    does not value benefits, select amortization periods, store or post entries.
    """
    company_id = getattr(args, "company_id", None)
    company = Table("company")
    company_row = conn.execute(
        Q.from_(company).select(company.default_currency).where(company.id == P()).get_sql(),
        (company_id,),
    ).fetchone() if company_id else None
    if company_row is None:
        err("An existing company-id is required")
    currency = getattr(args, "currency", None)
    if not currency or currency != company_row[0]:
        err("currency must match the company's default currency; no conversion is performed")
    benefit_type = getattr(args, "benefit_type", None)
    if benefit_type not in ("pension", "opeb"):
        err("benefit-type must be pension or opeb")
    dates = []
    for field in ("measurement_date", "reporting_date"):
        value = getattr(args, field, None)
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError):
            err(f"{field.replace('_', '-')} must be YYYY-MM-DD")
        if parsed.isoformat() != value:
            err(f"{field.replace('_', '-')} must be YYYY-MM-DD")
        dates.append(parsed)
    measurement, reporting = dates
    if reporting < measurement:
        err("reporting-date cannot precede measurement-date")
    reference = getattr(args, "review_reference", None)
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
        err("review-reference is required and must be at most 200 characters")
    total = _benefit_money(getattr(args, "total_benefit_liability", None), "total-benefit-liability")
    position = _benefit_money(getattr(args, "fiduciary_net_position", None), "fiduciary-net-position")
    expense = _benefit_money(getattr(args, "expense_before_deferrals", None), "expense-before-deferrals", signed=True)
    percent_text = getattr(args, "employer_share_percent", None)
    if not isinstance(percent_text, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?", percent_text) is None:
        err("employer-share-percent must be a decimal from 0 to 100 with at most six decimal places")
    percent = Decimal(percent_text)
    if percent > 100:
        err("employer-share-percent cannot exceed 100")
    raw = getattr(args, "benefit_deferrals", None)
    if not isinstance(raw, str) or len(raw) > 100000:
        err("benefit-deferrals must be an explicit JSON array, including [] when empty")
    try:
        deferrals = json.loads(raw)
    except (ValueError, TypeError):
        err("benefit-deferrals must be a JSON array")
    if not isinstance(deferrals, list) or len(deferrals) > 100:
        err("benefit-deferrals must contain at most 100 items")
    with localcontext() as context:
        context.prec = 60
        opening = {"outflow": Decimal("0"), "inflow": Decimal("0")}
        recognition = {"outflow": Decimal("0"), "inflow": Decimal("0")}
        identifiers = set()
        rows = []
        for item in deferrals:
            if not isinstance(item, dict) or set(item) != {"id", "direction", "opening_amount", "schedule"}:
                err("Each deferral requires only id, direction, opening_amount and schedule")
            identifier, direction = item["id"], item["direction"]
            if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 100 or identifier in identifiers:
                err("Deferral ids must be distinct nonempty strings of at most 100 characters")
            identifiers.add(identifier)
            if not isinstance(direction, str) or direction not in opening:
                err("Deferral direction must be outflow or inflow")
            amount = _benefit_money(item["opening_amount"], "deferral opening_amount")
            schedule = item["schedule"]
            if not isinstance(schedule, list) or len(schedule) > 100:
                err("Each deferral schedule must be an array of at most 100 annual amounts")
            scheduled = Decimal("0")
            current = Decimal("0")
            last_year = reporting.year - 1
            normalized = []
            for period in schedule:
                if not isinstance(period, dict) or set(period) != {"year", "amount"}:
                    err("Each scheduled period requires only year and amount")
                year = period["year"]
                if type(year) is not int or year <= last_year or year > 9999:
                    err("Schedule years must be increasing, distinct and not before the reporting year")
                last_year = year
                annual = _benefit_money(period["amount"], "scheduled amount")
                scheduled += annual
                if year == reporting.year:
                    current = annual
                normalized.append({"year": year, "amount": f"{annual:.2f}"})
            if scheduled != amount:
                err("Each schedule must allocate its entire opening_amount exactly")
            opening[direction] += amount
            recognition[direction] += current
            rows.append({"id": identifier, "direction": direction, "opening_amount": f"{amount:.2f}",
                         "current_year_recognition": f"{current:.2f}", "closing_amount": f"{amount - current:.2f}",
                         "schedule": normalized})
        plan_net = total - position
        employer_net = (plan_net * percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        adjustment = recognition["outflow"] - recognition["inflow"]
        ok({"report": "benefit_liability_calculation", "company_id": company_id, "currency": currency, "benefit_type": benefit_type,
            "measurement_date": measurement.isoformat(), "reporting_date": reporting.isoformat(),
            "measurement_lag_days": (reporting - measurement).days, "review_reference": reference.strip(),
            "total_benefit_liability": f"{total:.2f}", "fiduciary_net_position": f"{position:.2f}",
            "plan_net_liability": f"{plan_net:.2f}", "employer_share_percent": percent_text,
            "employer_net_liability": f"{employer_net:.2f}",
            "employer_liability": f"{max(employer_net, Decimal('0')):.2f}",
            "employer_asset": f"{max(-employer_net, Decimal('0')):.2f}",
            "deferred_outflow_opening": f"{opening['outflow']:.2f}",
            "deferred_inflow_opening": f"{opening['inflow']:.2f}",
            "deferred_outflow_closing": f"{opening['outflow'] - recognition['outflow']:.2f}",
            "deferred_inflow_closing": f"{opening['inflow'] - recognition['inflow']:.2f}",
            "expense_before_deferrals": f"{expense:.2f}", "deferral_expense_adjustment": f"{adjustment:.2f}",
            "expense_preview": f"{expense + adjustment:.2f}", "deferrals": rows,
            "result_kind": "calculation_only", "posted": False, "stored": False,
            "scope": "Operator-reviewed inputs and annual schedules only. No actuarial valuation, compliance certification, persistent measurement history or ledger posting."})


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------
ACTIONS = {
    "calculate-benefit-liability": calculate_benefit_liability,
    "standards-compliance-dashboard": standards_compliance_dashboard,
    "status": status_action,
}
