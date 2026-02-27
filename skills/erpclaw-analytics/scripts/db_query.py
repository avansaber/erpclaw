#!/usr/bin/env python3
"""ERPClaw Analytics Skill — db_query.py

Cross-module KPIs, financial ratios, trends, and dashboards.
Owns NO tables — reads from all installed skills. 100% read-only.
Gracefully degrades when optional skills are not installed.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import sqlite3
import sys
from decimal import Decimal, ROUND_HALF_UP

# Add shared lib to path
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH
from erpclaw_lib.decimal_utils import to_decimal, round_currency
from erpclaw_lib.dependencies import (
    table_exists,
    check_required_tables,
    check_optional_tables,
    skill_installed,
    TABLE_TO_SKILL,
)
from erpclaw_lib.validation import check_input_lengths
from erpclaw_lib.response import ok, err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _d(val) -> Decimal:
    """Convert a DB value (possibly None) to Decimal."""
    if val is None:
        return Decimal("0")
    return to_decimal(str(val))


def _s(d: Decimal) -> str:
    """Format a Decimal to string for output."""
    return str(round_currency(d))


def _pct(numerator: Decimal, denominator: Decimal) -> str:
    """Calculate percentage as string. Returns 'N/A' if denominator is zero."""
    if denominator == 0:
        return "N/A"
    result = (numerator / denominator * Decimal("100")).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return f"{result}%"


def _ratio(numerator: Decimal, denominator: Decimal) -> str:
    """Calculate ratio as string. Returns 'N/A' if denominator is zero."""
    if denominator == 0:
        return "N/A"
    result = (numerator / denominator).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return str(result)


def _parse_json_arg(value, name):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        err(f"Invalid JSON for --{name}: {value}")


def _require_company(args):
    if not args.company_id:
        err("--company-id is required")


def _require_dates(args):
    if not args.from_date:
        err("--from-date is required")
    if not args.to_date:
        err("--to-date is required")


def _require_as_of(args):
    if not args.as_of_date:
        err("--as-of-date is required")


# ---------------------------------------------------------------------------
# Module availability map
# ---------------------------------------------------------------------------

SKILL_MODULES = {
    "erpclaw-setup": {"tables": ["company"], "label": "Setup"},
    "erpclaw-gl": {"tables": ["account", "gl_entry"], "label": "General Ledger"},
    "erpclaw-journals": {"tables": ["journal_entry"], "label": "Journals"},
    "erpclaw-payments": {"tables": ["payment_entry", "payment_ledger_entry"], "label": "Payments"},
    "erpclaw-tax": {"tables": ["tax_template"], "label": "Tax"},
    "erpclaw-reports": {"tables": [], "label": "Reports"},
    "erpclaw-inventory": {"tables": ["item", "stock_ledger_entry"], "label": "Inventory"},
    "erpclaw-selling": {"tables": ["customer", "sales_invoice"], "label": "Selling"},
    "erpclaw-buying": {"tables": ["supplier", "purchase_invoice"], "label": "Buying"},
    "erpclaw-manufacturing": {"tables": ["bom", "work_order"], "label": "Manufacturing"},
    "erpclaw-hr": {"tables": ["employee", "department"], "label": "HR"},
    "erpclaw-payroll": {"tables": ["salary_slip", "payroll_run"], "label": "Payroll"},
    "erpclaw-projects": {"tables": ["project", "task"], "label": "Projects"},
    "erpclaw-assets": {"tables": ["asset"], "label": "Assets"},
    "erpclaw-quality": {"tables": ["quality_inspection"], "label": "Quality"},
    "erpclaw-crm": {"tables": ["lead", "opportunity"], "label": "CRM"},
    "erpclaw-support": {"tables": ["issue"], "label": "Support"},
    "erpclaw-billing": {"tables": ["meter", "rate_plan"], "label": "Billing"},
    "erpclaw-ai-engine": {"tables": ["anomaly"], "label": "AI Engine"},
}


def _check_modules(conn):
    """Return dict of skill_name -> installed boolean."""
    result = {}
    for skill, info in SKILL_MODULES.items():
        if not info["tables"]:
            result[skill] = True
            continue
        result[skill] = table_exists(conn, info["tables"][0])
    return result


# ---------------------------------------------------------------------------
# GL helper queries
# ---------------------------------------------------------------------------

def _get_account_balance(conn, company_id, root_type=None, account_type=None,
                         as_of_date=None, from_date=None, to_date=None):
    """Get net balance for accounts matching filters.

    For asset/expense accounts (debit_normal): balance = SUM(debit) - SUM(credit)
    For liability/equity/income accounts (credit_normal): balance = SUM(credit) - SUM(debit)

    Returns Decimal.
    """
    where = ["a.company_id = ?", "g.is_cancelled = 0", "a.is_group = 0"]
    params = [company_id]

    if root_type:
        if isinstance(root_type, (list, tuple)):
            placeholders = ",".join("?" * len(root_type))
            where.append(f"a.root_type IN ({placeholders})")
            params.extend(root_type)
        else:
            where.append("a.root_type = ?")
            params.append(root_type)

    if account_type:
        if isinstance(account_type, (list, tuple)):
            placeholders = ",".join("?" * len(account_type))
            where.append(f"a.account_type IN ({placeholders})")
            params.extend(account_type)
        else:
            where.append("a.account_type = ?")
            params.append(account_type)

    if as_of_date:
        where.append("g.posting_date <= ?")
        params.append(as_of_date)

    if from_date:
        where.append("g.posting_date >= ?")
        params.append(from_date)

    if to_date:
        where.append("g.posting_date <= ?")
        params.append(to_date)

    where_clause = " AND ".join(where)
    row = conn.execute(
        f"""SELECT
                COALESCE(decimal_sum(g.debit), '0') as total_debit,
                COALESCE(decimal_sum(g.credit), '0') as total_credit
            FROM gl_entry g
            JOIN account a ON g.account_id = a.id
            WHERE {where_clause}""",
        params,
    ).fetchone()

    total_debit = _d(row["total_debit"])
    total_credit = _d(row["total_credit"])

    # For debit-normal accounts (asset, expense), balance = debit - credit
    # For credit-normal accounts (liability, equity, income), balance = credit - debit
    if root_type:
        rt = root_type if isinstance(root_type, str) else root_type[0]
        if rt in ("liability", "equity", "income"):
            return total_credit - total_debit
    if account_type:
        at = account_type if isinstance(account_type, str) else account_type[0]
        if at in ("receivable", "payable", "revenue", "equity",
                   "payroll_payable", "tax"):
            return total_credit - total_debit

    return total_debit - total_credit


def _get_account_balances_grouped(conn, company_id, root_type, from_date, to_date,
                                  group_by="account"):
    """Get balances grouped by account or cost_center."""
    where = [
        "a.company_id = ?", "g.is_cancelled = 0", "a.is_group = 0",
        "a.root_type = ?", "g.posting_date >= ?", "g.posting_date <= ?"
    ]
    params = [company_id, root_type, from_date, to_date]

    if group_by == "cost_center":
        select_col = "COALESCE(cc.name, 'Unassigned') as group_name"
        join_clause = "LEFT JOIN cost_center cc ON g.cost_center_id = cc.id"
        group_col = "group_name"
    else:
        select_col = "a.name as group_name, a.account_number"
        join_clause = ""
        group_col = "a.id"

    where_clause = " AND ".join(where)
    rows = conn.execute(
        f"""SELECT {select_col},
                COALESCE(decimal_sum(g.debit), '0') as total_debit,
                COALESCE(decimal_sum(g.credit), '0') as total_credit
            FROM gl_entry g
            JOIN account a ON g.account_id = a.id
            {join_clause}
            WHERE {where_clause}
            GROUP BY {group_col}
            ORDER BY total_debit DESC""",
        params,
    ).fetchall()

    result = []
    for r in rows:
        debit = _d(r["total_debit"])
        credit = _d(r["total_credit"])
        if root_type in ("liability", "equity", "income"):
            balance = credit - debit
        else:
            balance = debit - credit
        if balance != 0:
            entry = {"name": r["group_name"], "amount": _s(balance)}
            if group_by == "account" and "account_number" in r.keys():
                entry["account_number"] = r["account_number"]
            result.append(entry)

    result.sort(key=lambda x: Decimal(x["amount"]), reverse=True)
    return result


def _get_period_breaks(from_date, to_date, periodicity="monthly"):
    """Generate period start/end date pairs."""
    from datetime import date, timedelta
    import calendar

    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    periods = []

    if periodicity == "quarterly":
        current = start.replace(day=1)
        while current <= end:
            q_month = current.month
            q_end_month = q_month + 2
            if q_end_month > 12:
                q_end_month = 12
            q_end_day = calendar.monthrange(current.year, q_end_month)[1]
            q_end = date(current.year, q_end_month, q_end_day)
            if q_end > end:
                q_end = end
            periods.append({
                "from_date": current.isoformat(),
                "to_date": q_end.isoformat(),
                "label": f"Q{(current.month - 1) // 3 + 1} {current.year}",
            })
            # Move to next quarter
            next_month = q_end_month + 1
            if next_month > 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, next_month, 1)
    elif periodicity == "annual":
        current = start.replace(day=1, month=1)
        while current <= end:
            year_end = date(current.year, 12, 31)
            if year_end > end:
                year_end = end
            p_start = current if current >= start else start
            periods.append({
                "from_date": p_start.isoformat(),
                "to_date": year_end.isoformat(),
                "label": str(current.year),
            })
            current = date(current.year + 1, 1, 1)
    else:
        # monthly (default)
        import calendar
        current = start.replace(day=1)
        while current <= end:
            month_end_day = calendar.monthrange(current.year, current.month)[1]
            month_end = date(current.year, current.month, month_end_day)
            if month_end > end:
                month_end = end
            p_start = current if current >= start else start
            periods.append({
                "from_date": p_start.isoformat(),
                "to_date": month_end.isoformat(),
                "label": current.strftime("%b %Y"),
            })
            # Move to next month
            if current.month == 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, current.month + 1, 1)

    return periods


# ===========================================================================
# ACTION: status
# ===========================================================================

def action_status(conn, args):
    """Show which ERPClaw modules are installed and available."""
    modules = _check_modules(conn)

    installed = []
    not_installed = []
    for skill, is_installed in modules.items():
        info = SKILL_MODULES[skill]
        entry = {"skill": skill, "label": info["label"]}
        if is_installed:
            installed.append(entry)
        else:
            not_installed.append(entry)

    result = {
        "installed_count": len(installed),
        "total_modules": len(SKILL_MODULES),
        "installed": installed,
        "not_installed": not_installed,
    }

    # If company provided, add company-level stats
    if args.company_id:
        gl_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()["cnt"]
        acct_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM account WHERE company_id = ? AND is_group = 0",
            (args.company_id,),
        ).fetchone()["cnt"]
        result["company_stats"] = {
            "gl_entries": gl_count,
            "accounts": acct_count,
        }

    ok(result)


# ===========================================================================
# ACTION: available-metrics
# ===========================================================================

# Map of action -> required skills (beyond setup+gl which are always required)
ACTION_REQUIREMENTS = {
    "status": [],
    "available-metrics": [],
    "liquidity-ratios": [],
    "profitability-ratios": [],
    "expense-breakdown": [],
    "cost-trend": [],
    "efficiency-ratios": ["erpclaw-selling", "erpclaw-buying", "erpclaw-inventory"],
    "revenue-by-customer": ["erpclaw-selling"],
    "revenue-by-item": ["erpclaw-selling"],
    "revenue-trend": [],  # Falls back to GL if selling missing
    "customer-concentration": ["erpclaw-selling"],
    "opex-vs-capex": [],  # Assets optional, estimates from GL
    "abc-analysis": ["erpclaw-inventory"],
    "inventory-turnover": ["erpclaw-inventory"],
    "aging-inventory": ["erpclaw-inventory"],
    "headcount-analytics": ["erpclaw-hr"],
    "payroll-analytics": ["erpclaw-payroll"],
    "leave-utilization": ["erpclaw-hr"],
    "project-profitability": ["erpclaw-projects"],
    "quality-dashboard": ["erpclaw-quality"],
    "support-metrics": ["erpclaw-support"],
    "executive-dashboard": [],
    "company-scorecard": [],
    "metric-trend": [],
    "period-comparison": [],
}


def action_available_metrics(conn, args):
    """List which analytics actions are available based on installed modules."""
    modules = _check_modules(conn)

    available = []
    unavailable = []

    for action_name, required_skills in ACTION_REQUIREMENTS.items():
        missing = [s for s in required_skills if not modules.get(s, False)]
        if missing:
            unavailable.append({
                "action": action_name,
                "available": False,
                "missing_skills": missing,
                "reason": f"Requires: {', '.join(missing)}",
            })
        else:
            available.append({
                "action": action_name,
                "available": True,
            })

    ok({
        "available_count": len(available),
        "unavailable_count": len(unavailable),
        "available": available,
        "unavailable": unavailable,
    })


# ===========================================================================
# ACTION: liquidity-ratios
# ===========================================================================

def action_liquidity_ratios(conn, args):
    """Compute liquidity ratios as of a given date.

    - Current Ratio = Current Assets / Current Liabilities
    - Quick Ratio = (Current Assets - Inventory) / Current Liabilities
    - Cash Ratio = Cash & Bank / Current Liabilities
    """
    _require_company(args)
    _require_as_of(args)

    company_id = args.company_id
    as_of = args.as_of_date

    # Current assets: asset accounts excluding fixed_asset and accumulated_depreciation
    current_asset_types = ("bank", "cash", "receivable", "stock",
                           "stock_received_not_billed", "stock_adjustment",
                           "asset_received_not_billed", "temporary")

    total_current_assets = Decimal("0")
    cash_and_bank = Decimal("0")
    inventory_value = Decimal("0")

    # Get all non-group asset accounts for this company
    asset_accounts = conn.execute(
        """SELECT id, account_type FROM account
           WHERE company_id = ? AND root_type = 'asset' AND is_group = 0""",
        (company_id,),
    ).fetchall()

    for acct in asset_accounts:
        at = acct["account_type"]
        if at in current_asset_types or at is None:
            # For asset accounts not explicitly typed as fixed, treat as current
            if at in ("fixed_asset", "accumulated_depreciation"):
                continue
            bal = _get_single_account_balance(conn, acct["id"], as_of, "asset")
            total_current_assets += bal
            if at in ("cash", "bank"):
                cash_and_bank += bal
            if at == "stock":
                inventory_value += bal

    # Current liabilities: liability accounts excluding long-term
    total_current_liabilities = Decimal("0")
    liability_accounts = conn.execute(
        """SELECT id, account_type FROM account
           WHERE company_id = ? AND root_type = 'liability' AND is_group = 0""",
        (company_id,),
    ).fetchall()

    for acct in liability_accounts:
        bal = _get_single_account_balance(conn, acct["id"], as_of, "liability")
        total_current_liabilities += bal

    quick_assets = total_current_assets - inventory_value

    ok({
        "as_of_date": as_of,
        "current_assets": _s(total_current_assets),
        "current_liabilities": _s(total_current_liabilities),
        "cash_and_bank": _s(cash_and_bank),
        "inventory": _s(inventory_value),
        "ratios": {
            "current_ratio": _ratio(total_current_assets, total_current_liabilities),
            "quick_ratio": _ratio(quick_assets, total_current_liabilities),
            "cash_ratio": _ratio(cash_and_bank, total_current_liabilities),
        },
        "interpretation": _interpret_liquidity(
            total_current_assets, total_current_liabilities,
            quick_assets, cash_and_bank
        ),
    })


def _get_single_account_balance(conn, account_id, as_of_date, root_type):
    """Get balance for a single account as of a date."""
    row = conn.execute(
        """SELECT COALESCE(decimal_sum(debit), '0') as d,
                  COALESCE(decimal_sum(credit), '0') as c
           FROM gl_entry
           WHERE account_id = ? AND posting_date <= ? AND is_cancelled = 0""",
        (account_id, as_of_date),
    ).fetchone()
    debit = _d(row["d"])
    credit = _d(row["c"])
    if root_type in ("liability", "equity", "income"):
        return credit - debit
    return debit - credit


def _interpret_liquidity(current_assets, current_liabilities, quick_assets, cash):
    """Provide plain-English interpretation of liquidity ratios."""
    if current_liabilities == 0:
        return "No current liabilities recorded."

    cr = current_assets / current_liabilities
    interpretations = []

    if cr >= Decimal("2"):
        interpretations.append("Strong liquidity — current assets well exceed current liabilities.")
    elif cr >= Decimal("1"):
        interpretations.append("Adequate liquidity — current assets cover current liabilities.")
    else:
        interpretations.append("Liquidity concern — current liabilities exceed current assets.")

    qr = quick_assets / current_liabilities
    if qr < Decimal("1"):
        interpretations.append("Quick ratio below 1.0 — may rely on inventory to meet obligations.")

    return " ".join(interpretations)


# ===========================================================================
# ACTION: profitability-ratios
# ===========================================================================

def action_profitability_ratios(conn, args):
    """Compute profitability ratios for a period.

    - Gross Margin = (Revenue - COGS) / Revenue
    - Net Profit Margin = Net Income / Revenue
    - Return on Assets (ROA) = Net Income / Total Assets
    - Return on Equity (ROE) = Net Income / Total Equity
    """
    _require_company(args)
    _require_dates(args)

    company_id = args.company_id
    from_date = args.from_date
    to_date = args.to_date

    # Revenue = income account balances for the period
    revenue = _get_account_balance(
        conn, company_id, root_type="income",
        from_date=from_date, to_date=to_date
    )

    # COGS = cost_of_goods_sold account type for the period
    cogs = _get_account_balance(
        conn, company_id, account_type="cost_of_goods_sold",
        from_date=from_date, to_date=to_date
    )

    # Total expenses for the period
    total_expenses = _get_account_balance(
        conn, company_id, root_type="expense",
        from_date=from_date, to_date=to_date
    )

    net_income = revenue - total_expenses

    # Total assets as of to_date (balance sheet item)
    total_assets = _get_account_balance(
        conn, company_id, root_type="asset", as_of_date=to_date
    )

    # Total equity as of to_date
    total_equity = _get_account_balance(
        conn, company_id, root_type="equity", as_of_date=to_date
    )

    gross_profit = revenue - cogs

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "revenue": _s(revenue),
        "cogs": _s(cogs),
        "gross_profit": _s(gross_profit),
        "total_expenses": _s(total_expenses),
        "net_income": _s(net_income),
        "total_assets": _s(total_assets),
        "total_equity": _s(total_equity),
        "ratios": {
            "gross_margin": _pct(gross_profit, revenue),
            "net_profit_margin": _pct(net_income, revenue),
            "roa": _pct(net_income, total_assets),
            "roe": _pct(net_income, total_equity),
        },
        "interpretation": _interpret_profitability(
            revenue, gross_profit, net_income, total_assets, total_equity
        ),
    })


def _interpret_profitability(revenue, gross_profit, net_income, total_assets, total_equity):
    """Provide plain-English interpretation of profitability ratios."""
    if revenue == 0:
        return "No revenue recorded for this period."

    parts = []
    gm = gross_profit / revenue * Decimal("100")
    if gm >= Decimal("50"):
        parts.append(f"Strong gross margin at {gm.quantize(Decimal('0.1'))}%.")
    elif gm >= Decimal("25"):
        parts.append(f"Moderate gross margin at {gm.quantize(Decimal('0.1'))}%.")
    elif gm > 0:
        parts.append(f"Low gross margin at {gm.quantize(Decimal('0.1'))}%.")

    if net_income > 0:
        parts.append("Company is profitable for the period.")
    elif net_income == 0:
        parts.append("Company broke even for the period.")
    else:
        parts.append("Company recorded a net loss for the period.")

    return " ".join(parts) if parts else "Insufficient data for interpretation."


# ===========================================================================
# ACTION: expense-breakdown
# ===========================================================================

def action_expense_breakdown(conn, args):
    """Break down expenses by account or cost center for a period."""
    _require_company(args)
    _require_dates(args)

    company_id = args.company_id
    from_date = args.from_date
    to_date = args.to_date
    group_by = getattr(args, "group_by", "account") or "account"

    if group_by not in ("account", "cost_center"):
        err("--group-by must be 'account' or 'cost_center'")

    items = _get_account_balances_grouped(
        conn, company_id, "expense", from_date, to_date, group_by
    )

    # Compute total
    total = sum(Decimal(i["amount"]) for i in items)

    # Add percentage to each item
    for item in items:
        item["percentage"] = _pct(Decimal(item["amount"]), total)

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "group_by": group_by,
        "total_expenses": _s(total),
        "breakdown": items,
        "count": len(items),
    })


# ===========================================================================
# ACTION: cost-trend
# ===========================================================================

def action_cost_trend(conn, args):
    """Show expense trends over time (monthly, quarterly, or annual)."""
    _require_company(args)
    _require_dates(args)

    company_id = args.company_id
    from_date = args.from_date
    to_date = args.to_date
    periodicity = getattr(args, "periodicity", "monthly") or "monthly"
    account_id = getattr(args, "account_id", None)

    periods = _get_period_breaks(from_date, to_date, periodicity)
    trend = []

    for p in periods:
        if account_id:
            # Single account trend
            bal = _get_single_account_balance_period(
                conn, account_id, p["from_date"], p["to_date"]
            )
        else:
            # All expenses
            bal = _get_account_balance(
                conn, company_id, root_type="expense",
                from_date=p["from_date"], to_date=p["to_date"]
            )
        trend.append({
            "period": p["label"],
            "from_date": p["from_date"],
            "to_date": p["to_date"],
            "amount": _s(bal),
        })

    # Compute period-over-period change
    for i in range(1, len(trend)):
        prev = Decimal(trend[i - 1]["amount"])
        curr = Decimal(trend[i]["amount"])
        if prev != 0:
            change = ((curr - prev) / prev * Decimal("100")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
            trend[i]["change_pct"] = f"{change}%"
        else:
            trend[i]["change_pct"] = "N/A"

    total = sum(Decimal(t["amount"]) for t in trend)
    avg = total / len(trend) if trend else Decimal("0")

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "periodicity": periodicity,
        "account_id": account_id,
        "total": _s(total),
        "average": _s(avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "periods": trend,
    })


def _get_single_account_balance_period(conn, account_id, from_date, to_date):
    """Get net movement for a single account in a period."""
    row = conn.execute(
        """SELECT COALESCE(decimal_sum(debit), '0') as d,
                  COALESCE(decimal_sum(credit), '0') as c
           FROM gl_entry
           WHERE account_id = ? AND posting_date >= ? AND posting_date <= ?
           AND is_cancelled = 0""",
        (account_id, from_date, to_date),
    ).fetchone()
    # For expense accounts, balance = debit - credit
    return _d(row["d"]) - _d(row["c"])


# ===========================================================================
# ACTION: efficiency-ratios
# ===========================================================================

def action_efficiency_ratios(conn, args):
    """Compute efficiency ratios (DSO, DPO, inventory turnover days).

    Gracefully degrades: skips ratios when required modules are missing.
    """
    _require_company(args)
    _require_dates(args)

    company_id = args.company_id
    from_date = args.from_date
    to_date = args.to_date
    modules = _check_modules(conn)

    ratios = {}
    notes = []

    # Revenue for the period (always available from GL)
    revenue = _get_account_balance(
        conn, company_id, root_type="income",
        from_date=from_date, to_date=to_date
    )

    # COGS
    cogs = _get_account_balance(
        conn, company_id, account_type="cost_of_goods_sold",
        from_date=from_date, to_date=to_date
    )

    # Days in period
    from datetime import date
    d1 = date.fromisoformat(from_date)
    d2 = date.fromisoformat(to_date)
    days_in_period = max((d2 - d1).days, 1)

    # DSO — Days Sales Outstanding (requires selling)
    if modules.get("erpclaw-selling"):
        ar_balance = _get_account_balance(
            conn, company_id, account_type="receivable", as_of_date=to_date
        )
        daily_revenue = revenue / Decimal(str(days_in_period)) if revenue > 0 else Decimal("0")
        if daily_revenue > 0:
            dso = (ar_balance / daily_revenue).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            ratios["dso"] = str(dso)
        else:
            ratios["dso"] = "N/A"
        ratios["ar_balance"] = _s(ar_balance)
    else:
        ratios["dso"] = None
        notes.append("DSO unavailable — erpclaw-selling not installed.")

    # DPO — Days Payable Outstanding (requires buying)
    if modules.get("erpclaw-buying"):
        ap_balance = _get_account_balance(
            conn, company_id, account_type="payable", as_of_date=to_date
        )
        daily_cogs = cogs / Decimal(str(days_in_period)) if cogs > 0 else Decimal("0")
        if daily_cogs > 0:
            dpo = (ap_balance / daily_cogs).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            ratios["dpo"] = str(dpo)
        else:
            ratios["dpo"] = "N/A"
        ratios["ap_balance"] = _s(ap_balance)
    else:
        ratios["dpo"] = None
        notes.append("DPO unavailable — erpclaw-buying not installed.")

    # Inventory Turnover Days (requires inventory)
    if modules.get("erpclaw-inventory"):
        inventory_balance = _get_account_balance(
            conn, company_id, account_type="stock", as_of_date=to_date
        )
        if cogs > 0:
            turnover_days = (inventory_balance / cogs * Decimal(str(days_in_period))).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
            ratios["inventory_turnover_days"] = str(turnover_days)
        else:
            ratios["inventory_turnover_days"] = "N/A"
        ratios["inventory_balance"] = _s(inventory_balance)
    else:
        ratios["inventory_turnover_days"] = None
        notes.append("Inventory turnover unavailable — erpclaw-inventory not installed.")

    # Asset turnover (always available from GL)
    total_assets = _get_account_balance(
        conn, company_id, root_type="asset", as_of_date=to_date
    )
    ratios["asset_turnover"] = _ratio(revenue, total_assets)

    result = {
        "period": {"from_date": from_date, "to_date": to_date},
        "days_in_period": days_in_period,
        "revenue": _s(revenue),
        "cogs": _s(cogs),
        "ratios": ratios,
    }
    if notes:
        result["notes"] = notes

    ok(result)


# ===========================================================================
# ACTION: revenue-by-customer
# ===========================================================================

def action_revenue_by_customer(conn, args):
    """Revenue breakdown by customer (requires erpclaw-selling)."""
    _require_company(args)
    _require_dates(args)

    dep = check_required_tables(conn, ["customer", "sales_invoice"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    limit = int(getattr(args, "limit", "20") or "20")
    offset = int(getattr(args, "offset", "0") or "0")

    rows = conn.execute(
        """SELECT c.id as customer_id, c.name as customer_name,
                  COUNT(si.id) as invoice_count,
                  COALESCE(decimal_sum(si.grand_total), '0') as total_revenue
           FROM sales_invoice si
           JOIN customer c ON si.customer_id = c.id
           WHERE c.company_id = ? AND si.status IN ('submitted', 'paid')
           AND si.posting_date >= ? AND si.posting_date <= ?
           GROUP BY c.id, c.name
           ORDER BY total_revenue DESC
           LIMIT ? OFFSET ?""",
        (company_id, args.from_date, args.to_date, limit, offset),
    ).fetchall()

    total_row = conn.execute(
        """SELECT COALESCE(decimal_sum(si.grand_total), '0') as total
           FROM sales_invoice si
           JOIN customer c ON si.customer_id = c.id
           WHERE c.company_id = ? AND si.status IN ('submitted', 'paid')
           AND si.posting_date >= ? AND si.posting_date <= ?""",
        (company_id, args.from_date, args.to_date),
    ).fetchone()

    grand_total = _d(total_row["total"])
    customers = []
    for r in rows:
        rev = _d(r["total_revenue"])
        customers.append({
            "customer_id": r["customer_id"],
            "customer_name": r["customer_name"],
            "invoice_count": r["invoice_count"],
            "revenue": _s(rev),
            "share": _pct(rev, grand_total),
        })

    ok({
        "period": {"from_date": args.from_date, "to_date": args.to_date},
        "grand_total": _s(grand_total),
        "customers": customers,
        "count": len(customers),
        "limit": limit,
        "offset": offset,
    })


# ===========================================================================
# ACTION: revenue-by-item
# ===========================================================================

def action_revenue_by_item(conn, args):
    """Revenue breakdown by item (requires erpclaw-selling)."""
    _require_company(args)
    _require_dates(args)

    dep = check_required_tables(conn, ["sales_invoice", "sales_invoice_item", "item"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    limit = int(getattr(args, "limit", "20") or "20")
    offset = int(getattr(args, "offset", "0") or "0")

    rows = conn.execute(
        """SELECT i.id as item_id, i.item_name as item_name,
                  decimal_sum(sii.quantity) as total_qty,
                  decimal_sum(sii.amount) as total_amount
           FROM sales_invoice_item sii
           JOIN sales_invoice si ON sii.sales_invoice_id = si.id
           JOIN item i ON sii.item_id = i.id
           JOIN customer c ON si.customer_id = c.id
           WHERE c.company_id = ? AND si.status IN ('submitted', 'paid')
           AND si.posting_date >= ? AND si.posting_date <= ?
           GROUP BY i.id, i.item_name
           ORDER BY total_amount DESC
           LIMIT ? OFFSET ?""",
        (company_id, args.from_date, args.to_date, limit, offset),
    ).fetchall()

    total_row = conn.execute(
        """SELECT COALESCE(decimal_sum(sii.amount), '0') as total
           FROM sales_invoice_item sii
           JOIN sales_invoice si ON sii.sales_invoice_id = si.id
           JOIN customer c ON si.customer_id = c.id
           WHERE c.company_id = ? AND si.status IN ('submitted', 'paid')
           AND si.posting_date >= ? AND si.posting_date <= ?""",
        (company_id, args.from_date, args.to_date),
    ).fetchone()

    grand_total = _d(total_row["total"])
    items = []
    for r in rows:
        amt = _d(r["total_amount"])
        items.append({
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "qty_sold": str(_d(r["total_qty"])),
            "revenue": _s(amt),
            "share": _pct(amt, grand_total),
        })

    ok({
        "period": {"from_date": args.from_date, "to_date": args.to_date},
        "grand_total": _s(grand_total),
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    })


# ===========================================================================
# ACTION: revenue-trend
# ===========================================================================

def action_revenue_trend(conn, args):
    """Revenue trend over time. Falls back to GL income if selling missing."""
    _require_company(args)
    _require_dates(args)

    company_id = args.company_id
    from_date = args.from_date
    to_date = args.to_date
    periodicity = getattr(args, "periodicity", "monthly") or "monthly"
    modules = _check_modules(conn)

    use_invoices = modules.get("erpclaw-selling", False)
    periods = _get_period_breaks(from_date, to_date, periodicity)
    trend = []

    for p in periods:
        if use_invoices:
            row = conn.execute(
                """SELECT COALESCE(decimal_sum(si.grand_total), '0') as total
                   FROM sales_invoice si
                   JOIN customer c ON si.customer_id = c.id
                   WHERE c.company_id = ? AND si.status IN ('submitted', 'paid')
                   AND si.posting_date >= ? AND si.posting_date <= ?""",
                (company_id, p["from_date"], p["to_date"]),
            ).fetchone()
            amount = _d(row["total"])
        else:
            amount = _get_account_balance(
                conn, company_id, root_type="income",
                from_date=p["from_date"], to_date=p["to_date"]
            )

        trend.append({
            "period": p["label"],
            "from_date": p["from_date"],
            "to_date": p["to_date"],
            "revenue": _s(amount),
        })

    # Period-over-period change
    for i in range(1, len(trend)):
        prev = Decimal(trend[i - 1]["revenue"])
        curr = Decimal(trend[i]["revenue"])
        if prev != 0:
            change = ((curr - prev) / prev * Decimal("100")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
            trend[i]["change_pct"] = f"{change}%"
        else:
            trend[i]["change_pct"] = "N/A"

    total = sum(Decimal(t["revenue"]) for t in trend)

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "periodicity": periodicity,
        "source": "sales_invoice" if use_invoices else "gl_income_accounts",
        "total_revenue": _s(total),
        "trend": trend,
    })


# ===========================================================================
# ACTION: customer-concentration
# ===========================================================================

def action_customer_concentration(conn, args):
    """Analyze customer revenue concentration (requires erpclaw-selling)."""
    _require_company(args)
    _require_dates(args)

    dep = check_required_tables(conn, ["customer", "sales_invoice"])
    if dep:
        err(dep["error"])

    company_id = args.company_id

    rows = conn.execute(
        """SELECT c.name as customer_name,
                  COALESCE(decimal_sum(si.grand_total), '0') as revenue
           FROM sales_invoice si
           JOIN customer c ON si.customer_id = c.id
           WHERE c.company_id = ? AND si.status IN ('submitted', 'paid')
           AND si.posting_date >= ? AND si.posting_date <= ?
           GROUP BY c.id, c.name
           ORDER BY revenue DESC""",
        (company_id, args.from_date, args.to_date),
    ).fetchall()

    total = sum(_d(r["revenue"]) for r in rows)
    if total == 0:
        ok({
            "period": {"from_date": args.from_date, "to_date": args.to_date},
            "total_revenue": "0.00",
            "customer_count": 0,
            "concentration": {},
            "top_customers": [],
            "interpretation": "No revenue recorded for this period.",
        })
        return

    # Compute cumulative share
    running = Decimal("0")
    top_customers = []
    top_1_share = Decimal("0")
    top_5_share = Decimal("0")
    top_10_share = Decimal("0")

    for i, r in enumerate(rows):
        rev = _d(r["revenue"])
        running += rev
        share = rev / total * Decimal("100")
        cum_share = running / total * Decimal("100")

        top_customers.append({
            "rank": i + 1,
            "customer": r["customer_name"],
            "revenue": _s(rev),
            "share": f"{share.quantize(Decimal('0.1'))}%",
            "cumulative_share": f"{cum_share.quantize(Decimal('0.1'))}%",
        })

        if i == 0:
            top_1_share = share
        if i < 5:
            top_5_share += share
        if i < 10:
            top_10_share += share

    ok({
        "period": {"from_date": args.from_date, "to_date": args.to_date},
        "total_revenue": _s(total),
        "customer_count": len(rows),
        "concentration": {
            "top_1_share": f"{top_1_share.quantize(Decimal('0.1'))}%",
            "top_5_share": f"{top_5_share.quantize(Decimal('0.1'))}%",
            "top_10_share": f"{top_10_share.quantize(Decimal('0.1'))}%",
        },
        "top_customers": top_customers[:10],
        "interpretation": _interpret_concentration(top_1_share, len(rows)),
    })


def _interpret_concentration(top_1_share, customer_count):
    """Interpret customer concentration risk."""
    if customer_count <= 1:
        return "Single customer — maximum concentration risk."
    if top_1_share >= Decimal("50"):
        return f"High concentration risk — top customer accounts for {top_1_share.quantize(Decimal('0.1'))}% of revenue."
    if top_1_share >= Decimal("25"):
        return f"Moderate concentration — top customer accounts for {top_1_share.quantize(Decimal('0.1'))}% of revenue."
    return "Revenue is well-diversified across customers."


# ===========================================================================
# ACTION: opex-vs-capex
# ===========================================================================

def action_opex_vs_capex(conn, args):
    """Compare operating expenses vs capital expenditure."""
    _require_company(args)
    _require_dates(args)

    company_id = args.company_id
    from_date = args.from_date
    to_date = args.to_date
    modules = _check_modules(conn)

    # OpEx = all expense accounts
    opex = _get_account_balance(
        conn, company_id, root_type="expense",
        from_date=from_date, to_date=to_date
    )

    # CapEx: if assets module installed, use fixed_asset additions
    # Otherwise estimate from GL entries to fixed_asset accounts
    capex = Decimal("0")
    capex_source = "gl_fixed_asset_accounts"

    capex = _get_account_balance(
        conn, company_id, account_type="fixed_asset",
        from_date=from_date, to_date=to_date
    )

    total = opex + capex
    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "opex": _s(opex),
        "capex": _s(capex),
        "total": _s(total),
        "opex_share": _pct(opex, total),
        "capex_share": _pct(capex, total),
        "capex_source": capex_source,
        "assets_module": modules.get("erpclaw-assets", False),
    })


# ===========================================================================
# ACTION: abc-analysis
# ===========================================================================

def action_abc_analysis(conn, args):
    """ABC inventory classification (requires erpclaw-inventory)."""
    _require_company(args)

    dep = check_required_tables(conn, ["item", "stock_ledger_entry"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    as_of = getattr(args, "as_of_date", None)

    where = ["w.company_id = ?", "sle.is_cancelled = 0"]
    params = [company_id]
    if as_of:
        where.append("sle.posting_date <= ?")
        params.append(as_of)

    rows = conn.execute(
        f"""SELECT i.id as item_id, i.item_name as item_name,
                   COALESCE(decimal_sum(sle.stock_value_difference), '0') as value
            FROM stock_ledger_entry sle
            JOIN item i ON sle.item_id = i.id
            JOIN warehouse w ON sle.warehouse_id = w.id
            WHERE {' AND '.join(where)}
            GROUP BY i.id, i.item_name
            HAVING value + 0 > 0
            ORDER BY value DESC""",
        params,
    ).fetchall()

    total = sum(_d(r["value"]) for r in rows)
    if total == 0:
        ok({
            "as_of_date": as_of,
            "total_value": "0.00",
            "items": [],
            "summary": {"A": 0, "B": 0, "C": 0},
        })
        return

    running = Decimal("0")
    items = []
    summary = {"A": 0, "B": 0, "C": 0}
    for r in rows:
        val = _d(r["value"])
        running += val
        cum_pct = running / total * Decimal("100")
        if cum_pct <= Decimal("80"):
            grade = "A"
        elif cum_pct <= Decimal("95"):
            grade = "B"
        else:
            grade = "C"
        summary[grade] += 1
        items.append({
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "value": _s(val),
            "share": _pct(val, total),
            "cumulative": f"{cum_pct.quantize(Decimal('0.1'))}%",
            "class": grade,
        })

    ok({
        "as_of_date": as_of,
        "total_value": _s(total),
        "item_count": len(items),
        "summary": summary,
        "items": items,
    })


# ===========================================================================
# ACTION: inventory-turnover
# ===========================================================================

def action_inventory_turnover(conn, args):
    """Inventory turnover analysis (requires erpclaw-inventory)."""
    _require_company(args)
    _require_dates(args)

    dep = check_required_tables(conn, ["item", "stock_ledger_entry"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    from_date = args.from_date
    to_date = args.to_date
    item_id = getattr(args, "item_id", None)
    warehouse_id = getattr(args, "warehouse_id", None)

    # COGS for the period
    cogs = _get_account_balance(
        conn, company_id, account_type="cost_of_goods_sold",
        from_date=from_date, to_date=to_date
    )

    # Average inventory from stock accounts
    inv_start = _get_account_balance(
        conn, company_id, account_type="stock", as_of_date=from_date
    )
    inv_end = _get_account_balance(
        conn, company_id, account_type="stock", as_of_date=to_date
    )
    avg_inventory = (inv_start + inv_end) / Decimal("2")

    from datetime import date
    days = max((date.fromisoformat(to_date) - date.fromisoformat(from_date)).days, 1)

    if avg_inventory > 0 and cogs > 0:
        turnover_ratio = cogs / avg_inventory
        turnover_days = (avg_inventory / cogs * Decimal(str(days))).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    else:
        turnover_ratio = Decimal("0")
        turnover_days = Decimal("0")

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "cogs": _s(cogs),
        "inventory_start": _s(inv_start),
        "inventory_end": _s(inv_end),
        "average_inventory": _s(avg_inventory),
        "turnover_ratio": _ratio(cogs, avg_inventory),
        "turnover_days": str(turnover_days),
        "days_in_period": days,
    })


# ===========================================================================
# ACTION: aging-inventory
# ===========================================================================

def action_aging_inventory(conn, args):
    """Aging inventory analysis (requires erpclaw-inventory)."""
    _require_company(args)
    _require_as_of(args)

    dep = check_required_tables(conn, ["item", "stock_ledger_entry"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    as_of = args.as_of_date
    buckets_str = getattr(args, "aging_buckets", "30,60,90,120") or "30,60,90,120"
    bucket_limits = [int(b) for b in buckets_str.split(",")]

    # Get latest SLE per item with positive qty
    rows = conn.execute(
        """SELECT i.id as item_id, i.item_name as item_name,
                  MAX(sle.posting_date) as last_movement,
                  decimal_sum(sle.actual_qty) as qty_balance,
                  decimal_sum(sle.stock_value_difference) as value
           FROM stock_ledger_entry sle
           JOIN item i ON sle.item_id = i.id
           JOIN warehouse w ON sle.warehouse_id = w.id
           WHERE w.company_id = ? AND sle.posting_date <= ? AND sle.is_cancelled = 0
           GROUP BY i.id, i.item_name
           HAVING qty_balance + 0 > 0""",
        (company_id, as_of),
    ).fetchall()

    from datetime import date
    as_of_dt = date.fromisoformat(as_of)

    items = []
    bucket_totals = {}
    for r in rows:
        last_move = date.fromisoformat(r["last_movement"])
        age_days = (as_of_dt - last_move).days
        bucket = _get_bucket_label(age_days, bucket_limits)
        val = _d(r["value"])
        items.append({
            "item_id": r["item_id"],
            "item_name": r["item_name"],
            "qty": str(_d(r["qty_balance"])),
            "value": _s(val),
            "age_days": age_days,
            "last_movement": r["last_movement"],
            "bucket": bucket,
        })
        bucket_totals[bucket] = bucket_totals.get(bucket, Decimal("0")) + val

    items.sort(key=lambda x: x["age_days"], reverse=True)
    total_value = sum(Decimal(i["value"]) for i in items)

    ok({
        "as_of_date": as_of,
        "aging_buckets": buckets_str,
        "total_items": len(items),
        "total_value": _s(total_value),
        "bucket_summary": {k: _s(v) for k, v in sorted(bucket_totals.items())},
        "items": items,
    })


def _get_bucket_label(age_days, bucket_limits):
    """Map age in days to a bucket label."""
    prev = 0
    for limit in sorted(bucket_limits):
        if age_days <= limit:
            return f"{prev + 1}-{limit} days" if prev > 0 else f"0-{limit} days"
        prev = limit
    return f"{bucket_limits[-1]}+ days"


# ===========================================================================
# ACTION: headcount-analytics
# ===========================================================================

def action_headcount_analytics(conn, args):
    """Employee headcount analysis (requires erpclaw-hr)."""
    _require_company(args)

    dep = check_required_tables(conn, ["employee"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    as_of = getattr(args, "as_of_date", None)
    group_by = getattr(args, "group_by", "department") or "department"

    where = ["e.company_id = ?", "e.status = 'active'"]
    params = [company_id]
    if as_of:
        where.append("e.date_of_joining <= ?")
        params.append(as_of)

    where_clause = " AND ".join(where)

    # Total headcount
    total = conn.execute(
        f"SELECT COUNT(*) as cnt FROM employee e WHERE {where_clause}",
        params,
    ).fetchone()["cnt"]

    # Group by department or designation
    if group_by == "department" and table_exists(conn, "department"):
        breakdown_rows = conn.execute(
            f"""SELECT COALESCE(d.name, 'Unassigned') as group_name, COUNT(*) as cnt
                FROM employee e
                LEFT JOIN department d ON e.department_id = d.id
                WHERE {where_clause}
                GROUP BY group_name
                ORDER BY cnt DESC""",
            params,
        ).fetchall()
    else:
        breakdown_rows = conn.execute(
            f"""SELECT COALESCE(e.status, 'Unknown') as group_name, COUNT(*) as cnt
                FROM employee e
                WHERE {where_clause}
                GROUP BY group_name
                ORDER BY cnt DESC""",
            params,
        ).fetchall()

    breakdown = []
    for r in breakdown_rows:
        breakdown.append({
            "name": r["group_name"],
            "count": r["cnt"],
            "share": _pct(Decimal(str(r["cnt"])), Decimal(str(total))) if total > 0 else "N/A",
        })

    ok({
        "as_of_date": as_of,
        "group_by": group_by,
        "total_headcount": total,
        "breakdown": breakdown,
    })


# ===========================================================================
# ACTION: payroll-analytics
# ===========================================================================

def action_payroll_analytics(conn, args):
    """Payroll cost analysis (requires erpclaw-payroll)."""
    _require_company(args)
    _require_dates(args)

    dep = check_required_tables(conn, ["payroll_run", "salary_slip"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    from_date = args.from_date
    to_date = args.to_date
    department_id = getattr(args, "department_id", None)

    where = [
        "pr.company_id = ?", "pr.status = 'submitted'",
        "pr.period_start >= ?", "pr.period_end <= ?"
    ]
    params = [company_id, from_date, to_date]

    if department_id:
        where.append("pr.department_id = ?")
        params.append(department_id)

    where_clause = " AND ".join(where)

    row = conn.execute(
        f"""SELECT COUNT(DISTINCT ss.id) as slip_count,
                   COALESCE(decimal_sum(ss.gross_pay), '0') as total_gross,
                   COALESCE(decimal_sum(ss.net_pay), '0') as total_net,
                   COALESCE(decimal_sum(ss.total_deductions), '0') as total_deductions
            FROM salary_slip ss
            JOIN payroll_run pr ON ss.payroll_run_id = pr.id
            WHERE {where_clause}""",
        params,
    ).fetchone()

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "slip_count": row["slip_count"],
        "total_gross": _s(_d(row["total_gross"])),
        "total_net": _s(_d(row["total_net"])),
        "total_deductions": _s(_d(row["total_deductions"])),
        "avg_gross_per_employee": _s(
            (_d(row["total_gross"]) / Decimal(str(row["slip_count"]))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ) if row["slip_count"] > 0 else Decimal("0")
        ),
    })


# ===========================================================================
# ACTION: leave-utilization
# ===========================================================================

def action_leave_utilization(conn, args):
    """Leave utilization analysis (requires erpclaw-hr)."""
    _require_company(args)

    dep = check_required_tables(conn, ["employee", "leave_allocation", "leave_application"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    from_date = getattr(args, "from_date", None)
    to_date = getattr(args, "to_date", None)

    # Total allocated leaves
    alloc_where = ["e.company_id = ?"]
    alloc_params = [company_id]

    alloc_row = conn.execute(
        f"""SELECT COALESCE(decimal_sum(la.total_leaves), '0') as total_allocated
            FROM leave_allocation la
            JOIN employee e ON la.employee_id = e.id
            WHERE {' AND '.join(alloc_where)}""",
        alloc_params,
    ).fetchone()

    # Used leaves (approved applications)
    used_where = ["e.company_id = ?", "lapp.status = 'approved'"]
    used_params = [company_id]
    if from_date:
        used_where.append("lapp.from_date >= ?")
        used_params.append(from_date)
    if to_date:
        used_where.append("lapp.to_date <= ?")
        used_params.append(to_date)

    used_row = conn.execute(
        f"""SELECT COALESCE(decimal_sum(lapp.total_days), '0') as total_used
            FROM leave_application lapp
            JOIN employee e ON lapp.employee_id = e.id
            WHERE {' AND '.join(used_where)}""",
        used_params,
    ).fetchone()

    total_allocated = _d(alloc_row["total_allocated"])
    total_used = _d(used_row["total_used"])
    utilization = _pct(total_used, total_allocated)

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "total_allocated": _s(total_allocated),
        "total_used": _s(total_used),
        "remaining": _s(total_allocated - total_used),
        "utilization": utilization,
    })


# ===========================================================================
# ACTION: project-profitability
# ===========================================================================

def action_project_profitability(conn, args):
    """Project profitability analysis (requires erpclaw-projects)."""
    _require_company(args)

    dep = check_required_tables(conn, ["project"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    project_id = getattr(args, "project_id", None)
    from_date = getattr(args, "from_date", None)
    to_date = getattr(args, "to_date", None)

    where = ["p.company_id = ?"]
    params = [company_id]

    if project_id:
        where.append("p.id = ?")
        params.append(project_id)
    if from_date:
        where.append("p.start_date >= ?")
        params.append(from_date)

    where_clause = " AND ".join(where)

    projects = conn.execute(
        f"""SELECT p.id, p.project_name, p.status,
                   COALESCE(p.estimated_cost + 0, 0) as est_cost,
                   COALESCE(p.actual_cost + 0, 0) as act_cost,
                   COALESCE(p.total_billed + 0, 0) as total_billed,
                   COALESCE(p.profit_margin + 0, 0) as profit_margin
            FROM project p
            WHERE {where_clause}
            ORDER BY p.project_name""",
        params,
    ).fetchall()

    result = []
    for proj in projects:
        est_cost = _d(proj["est_cost"])
        act_cost = _d(proj["act_cost"])
        total_billed = _d(proj["total_billed"])
        profit = total_billed - act_cost
        margin = _pct(profit, total_billed) if total_billed > 0 else "N/A"
        cost_var = _s(act_cost - est_cost) if est_cost > 0 else "N/A"

        result.append({
            "project_id": proj["id"],
            "project_name": proj["project_name"],
            "status": proj["status"],
            "estimated_cost": _s(est_cost),
            "actual_cost": _s(act_cost),
            "total_billed": _s(total_billed),
            "profit": _s(profit),
            "margin": margin,
            "cost_variance": cost_var,
        })

    ok({
        "project_count": len(result),
        "projects": result,
    })


# ===========================================================================
# ACTION: quality-dashboard
# ===========================================================================

def action_quality_dashboard(conn, args):
    """Quality metrics dashboard (requires erpclaw-quality)."""
    _require_company(args)

    dep = check_required_tables(conn, ["quality_inspection"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    from_date = getattr(args, "from_date", None)
    to_date = getattr(args, "to_date", None)

    # quality_inspection has no company_id — filter through item if needed
    where = ["1=1"]
    params = []
    if from_date:
        where.append("qi.inspection_date >= ?")
        params.append(from_date)
    if to_date:
        where.append("qi.inspection_date <= ?")
        params.append(to_date)

    where_clause = " AND ".join(where)

    # Inspection summary (status: accepted = pass, rejected = fail)
    row = conn.execute(
        f"""SELECT COUNT(*) as total,
                   SUM(CASE WHEN qi.status = 'accepted' THEN 1 ELSE 0 END) as passed,
                   SUM(CASE WHEN qi.status = 'rejected' THEN 1 ELSE 0 END) as failed
            FROM quality_inspection qi
            WHERE {where_clause}""",
        params,
    ).fetchone()

    total = row["total"]
    passed = row["passed"] or 0
    failed = row["failed"] or 0
    pass_rate = _pct(Decimal(str(passed)), Decimal(str(total))) if total > 0 else "N/A"

    # Non-conformance count if table exists
    nc_count = 0
    if table_exists(conn, "non_conformance"):
        nc_where = ["1=1"]
        nc_params = []
        if from_date:
            nc_where.append("nc.created_at >= ?")
            nc_params.append(from_date)
        if to_date:
            nc_where.append("nc.created_at <= ?")
            nc_params.append(to_date)
        nc_row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM non_conformance nc WHERE {' AND '.join(nc_where)}",
            nc_params,
        ).fetchone()
        nc_count = nc_row["cnt"]

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "inspections": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": pass_rate,
        },
        "non_conformances": nc_count,
    })


# ===========================================================================
# ACTION: support-metrics
# ===========================================================================

def action_support_metrics(conn, args):
    """Support ticket metrics (requires erpclaw-support)."""
    _require_company(args)

    dep = check_required_tables(conn, ["issue"])
    if dep:
        err(dep["error"])

    company_id = args.company_id
    from_date = getattr(args, "from_date", None)
    to_date = getattr(args, "to_date", None)

    # issue has no company_id — filter through customer_id
    company_filter = "AND i.customer_id IN (SELECT id FROM customer WHERE company_id = ?)"
    where = [f"1=1 {company_filter}"]
    params = [company_id]
    if from_date:
        where.append("i.created_at >= ?")
        params.append(from_date)
    if to_date:
        where.append("i.created_at <= ?")
        params.append(to_date)

    where_clause = " AND ".join(where)

    row = conn.execute(
        f"""SELECT COUNT(*) as total,
                   SUM(CASE WHEN i.status = 'open' THEN 1 ELSE 0 END) as open_count,
                   SUM(CASE WHEN i.status = 'resolved' THEN 1 ELSE 0 END) as resolved_count,
                   SUM(CASE WHEN i.status = 'closed' THEN 1 ELSE 0 END) as closed_count
            FROM issue i
            WHERE {where_clause}""",
        params,
    ).fetchone()

    total = row["total"]
    open_count = row["open_count"] or 0
    resolved = row["resolved_count"] or 0
    closed = row["closed_count"] or 0
    resolution_rate = _pct(
        Decimal(str(resolved + closed)), Decimal(str(total))
    ) if total > 0 else "N/A"

    # Priority breakdown
    priority_rows = conn.execute(
        f"""SELECT COALESCE(i.priority, 'Unset') as priority, COUNT(*) as cnt
            FROM issue i
            WHERE {where_clause}
            GROUP BY priority
            ORDER BY cnt DESC""",
        params,
    ).fetchall()

    priorities = [{"priority": r["priority"], "count": r["cnt"]} for r in priority_rows]

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "total_issues": total,
        "open": open_count,
        "resolved": resolved,
        "closed": closed,
        "resolution_rate": resolution_rate,
        "by_priority": priorities,
    })


# ===========================================================================
# ACTION: executive-dashboard
# ===========================================================================

def action_executive_dashboard(conn, args):
    """Executive dashboard — cross-module KPI summary.

    Gracefully degrades: each section independently checks module availability.
    """
    _require_company(args)

    company_id = args.company_id
    from_date = getattr(args, "from_date", None)
    to_date = getattr(args, "to_date", None)
    modules = _check_modules(conn)

    # Default dates if not provided
    if not from_date or not to_date:
        from datetime import date
        today = date.today()
        if not to_date:
            to_date = today.isoformat()
        if not from_date:
            from_date = today.replace(month=1, day=1).isoformat()

    dashboard = {}

    # Section 1: Financial summary (always available)
    revenue = _get_account_balance(
        conn, company_id, root_type="income",
        from_date=from_date, to_date=to_date
    )
    expenses = _get_account_balance(
        conn, company_id, root_type="expense",
        from_date=from_date, to_date=to_date
    )
    net_income = revenue - expenses
    total_assets = _get_account_balance(
        conn, company_id, root_type="asset", as_of_date=to_date
    )
    total_liabilities = _get_account_balance(
        conn, company_id, root_type="liability", as_of_date=to_date
    )

    dashboard["financial"] = {
        "available": True,
        "revenue": _s(revenue),
        "expenses": _s(expenses),
        "net_income": _s(net_income),
        "net_margin": _pct(net_income, revenue),
        "total_assets": _s(total_assets),
        "total_liabilities": _s(total_liabilities),
    }

    # Section 2: Selling (optional)
    if modules.get("erpclaw-selling"):
        inv_row = conn.execute(
            """SELECT COUNT(*) as cnt,
                      COALESCE(decimal_sum(si.grand_total), '0') as total
               FROM sales_invoice si
               JOIN customer c ON si.customer_id = c.id
               WHERE c.company_id = ? AND si.status IN ('submitted', 'paid')
               AND si.posting_date >= ? AND si.posting_date <= ?""",
            (company_id, from_date, to_date),
        ).fetchone()
        ar = _get_account_balance(
            conn, company_id, account_type="receivable", as_of_date=to_date
        )
        dashboard["selling"] = {
            "available": True,
            "invoices": inv_row["cnt"],
            "invoice_total": _s(_d(inv_row["total"])),
            "ar_outstanding": _s(ar),
        }
    else:
        dashboard["selling"] = {"available": False, "reason": "erpclaw-selling not installed"}

    # Section 3: Buying (optional)
    if modules.get("erpclaw-buying"):
        ap = _get_account_balance(
            conn, company_id, account_type="payable", as_of_date=to_date
        )
        dashboard["buying"] = {
            "available": True,
            "ap_outstanding": _s(ap),
        }
    else:
        dashboard["buying"] = {"available": False, "reason": "erpclaw-buying not installed"}

    # Section 4: Inventory (optional)
    if modules.get("erpclaw-inventory"):
        inv_val = _get_account_balance(
            conn, company_id, account_type="stock", as_of_date=to_date
        )
        dashboard["inventory"] = {
            "available": True,
            "stock_value": _s(inv_val),
        }
    else:
        dashboard["inventory"] = {"available": False, "reason": "erpclaw-inventory not installed"}

    # Section 5: HR (optional)
    if modules.get("erpclaw-hr"):
        emp_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM employee WHERE company_id = ? AND status = 'active'",
            (company_id,),
        ).fetchone()["cnt"]
        dashboard["hr"] = {
            "available": True,
            "active_employees": emp_count,
        }
    else:
        dashboard["hr"] = {"available": False, "reason": "erpclaw-hr not installed"}

    # Section 6: Support (optional)
    if modules.get("erpclaw-support") and table_exists(conn, "customer"):
        open_issues = conn.execute(
            """SELECT COUNT(*) as cnt FROM issue
               WHERE status = 'open'
               AND customer_id IN (SELECT id FROM customer WHERE company_id = ?)""",
            (company_id,),
        ).fetchone()["cnt"]
        dashboard["support"] = {
            "available": True,
            "open_issues": open_issues,
        }
    else:
        dashboard["support"] = {"available": False, "reason": "erpclaw-support not installed"}

    ok({
        "period": {"from_date": from_date, "to_date": to_date},
        "sections": dashboard,
    })


# ===========================================================================
# ACTION: company-scorecard
# ===========================================================================

def action_company_scorecard(conn, args):
    """Grade company performance across dimensions."""
    _require_company(args)

    company_id = args.company_id
    as_of = getattr(args, "as_of_date", None)
    modules = _check_modules(conn)

    from datetime import date
    if not as_of:
        as_of = date.today().isoformat()
    # Use current year as period
    year_start = date.fromisoformat(as_of).replace(month=1, day=1).isoformat()

    grades = {}

    # Liquidity grade
    current_assets = _get_account_balance(
        conn, company_id, root_type="asset", as_of_date=as_of
    )
    current_liabilities = _get_account_balance(
        conn, company_id, root_type="liability", as_of_date=as_of
    )
    if current_liabilities > 0:
        cr = current_assets / current_liabilities
        if cr >= Decimal("2"):
            grades["liquidity"] = {"grade": "A", "ratio": _ratio(current_assets, current_liabilities)}
        elif cr >= Decimal("1.5"):
            grades["liquidity"] = {"grade": "B", "ratio": _ratio(current_assets, current_liabilities)}
        elif cr >= Decimal("1"):
            grades["liquidity"] = {"grade": "C", "ratio": _ratio(current_assets, current_liabilities)}
        else:
            grades["liquidity"] = {"grade": "D", "ratio": _ratio(current_assets, current_liabilities)}
    else:
        grades["liquidity"] = {"grade": "N/A", "reason": "No liabilities recorded"}

    # Profitability grade
    revenue = _get_account_balance(
        conn, company_id, root_type="income",
        from_date=year_start, to_date=as_of
    )
    expenses = _get_account_balance(
        conn, company_id, root_type="expense",
        from_date=year_start, to_date=as_of
    )
    net_income = revenue - expenses
    if revenue > 0:
        margin = net_income / revenue * Decimal("100")
        if margin >= Decimal("20"):
            grades["profitability"] = {"grade": "A", "net_margin": f"{margin.quantize(Decimal('0.1'))}%"}
        elif margin >= Decimal("10"):
            grades["profitability"] = {"grade": "B", "net_margin": f"{margin.quantize(Decimal('0.1'))}%"}
        elif margin >= Decimal("0"):
            grades["profitability"] = {"grade": "C", "net_margin": f"{margin.quantize(Decimal('0.1'))}%"}
        else:
            grades["profitability"] = {"grade": "D", "net_margin": f"{margin.quantize(Decimal('0.1'))}%"}
    else:
        grades["profitability"] = {"grade": "N/A", "reason": "No revenue recorded"}

    # Selling grade (optional)
    if modules.get("erpclaw-selling"):
        ar = _get_account_balance(
            conn, company_id, account_type="receivable", as_of_date=as_of
        )
        if revenue > 0:
            ar_pct = ar / revenue * Decimal("100")
            if ar_pct <= Decimal("15"):
                grades["collections"] = {"grade": "A", "ar_to_revenue": f"{ar_pct.quantize(Decimal('0.1'))}%"}
            elif ar_pct <= Decimal("30"):
                grades["collections"] = {"grade": "B", "ar_to_revenue": f"{ar_pct.quantize(Decimal('0.1'))}%"}
            else:
                grades["collections"] = {"grade": "C", "ar_to_revenue": f"{ar_pct.quantize(Decimal('0.1'))}%"}
        else:
            grades["collections"] = {"grade": "N/A", "reason": "No revenue"}
    else:
        grades["collections"] = {"grade": "N/A", "reason": "erpclaw-selling not installed"}

    # HR grade (optional)
    if modules.get("erpclaw-hr"):
        emp_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM employee WHERE company_id = ? AND status = 'active'",
            (company_id,),
        ).fetchone()["cnt"]
        if emp_count > 0 and revenue > 0:
            rev_per_emp = revenue / Decimal(str(emp_count))
            grades["workforce"] = {
                "grade": "B" if rev_per_emp > Decimal("100000") else "C",
                "revenue_per_employee": _s(rev_per_emp.quantize(Decimal("0.01"))),
                "headcount": emp_count,
            }
        else:
            grades["workforce"] = {"grade": "N/A", "reason": "Insufficient data"}
    else:
        grades["workforce"] = {"grade": "N/A", "reason": "erpclaw-hr not installed"}

    # Overall grade
    letter_values = {"A": 4, "B": 3, "C": 2, "D": 1}
    scored = [letter_values[g["grade"]] for g in grades.values() if g["grade"] in letter_values]
    if scored:
        avg_score = sum(scored) / len(scored)
        if avg_score >= 3.5:
            overall = "A"
        elif avg_score >= 2.5:
            overall = "B"
        elif avg_score >= 1.5:
            overall = "C"
        else:
            overall = "D"
    else:
        overall = "N/A"

    ok({
        "as_of_date": as_of,
        "period": {"from_date": year_start, "to_date": as_of},
        "overall_grade": overall,
        "dimensions": grades,
    })


# ===========================================================================
# ACTION: metric-trend
# ===========================================================================

def action_metric_trend(conn, args):
    """Track a specific metric over time."""
    _require_company(args)

    metric = getattr(args, "metric", None)
    if not metric:
        err("--metric is required (e.g., revenue, expenses, net_income, headcount)")

    company_id = args.company_id
    from_date = getattr(args, "from_date", None)
    to_date = getattr(args, "to_date", None)
    periodicity = getattr(args, "periodicity", "monthly") or "monthly"
    modules = _check_modules(conn)

    from datetime import date as dt_date
    if not to_date:
        to_date = dt_date.today().isoformat()
    if not from_date:
        from_date = dt_date.fromisoformat(to_date).replace(month=1, day=1).isoformat()

    periods = _get_period_breaks(from_date, to_date, periodicity)

    METRIC_HANDLERS = {
        "revenue": lambda p: _get_account_balance(conn, company_id, root_type="income", from_date=p["from_date"], to_date=p["to_date"]),
        "expenses": lambda p: _get_account_balance(conn, company_id, root_type="expense", from_date=p["from_date"], to_date=p["to_date"]),
        "net_income": lambda p: (
            _get_account_balance(conn, company_id, root_type="income", from_date=p["from_date"], to_date=p["to_date"])
            - _get_account_balance(conn, company_id, root_type="expense", from_date=p["from_date"], to_date=p["to_date"])
        ),
        "assets": lambda p: _get_account_balance(conn, company_id, root_type="asset", as_of_date=p["to_date"]),
        "liabilities": lambda p: _get_account_balance(conn, company_id, root_type="liability", as_of_date=p["to_date"]),
    }

    # HR metrics (optional)
    if metric == "headcount":
        if not modules.get("erpclaw-hr"):
            err("headcount metric requires erpclaw-hr to be installed.")
        METRIC_HANDLERS["headcount"] = lambda p: Decimal(str(conn.execute(
            "SELECT COUNT(*) as cnt FROM employee WHERE company_id = ? AND status = 'active' AND date_of_joining <= ?",
            (company_id, p["to_date"]),
        ).fetchone()["cnt"]))

    if metric not in METRIC_HANDLERS:
        err(f"Unknown metric: {metric}. Available: {', '.join(sorted(METRIC_HANDLERS.keys()))}")

    handler = METRIC_HANDLERS[metric]
    trend = []
    for p in periods:
        val = handler(p)
        trend.append({
            "period": p["label"],
            "from_date": p["from_date"],
            "to_date": p["to_date"],
            "value": _s(val) if not isinstance(val, int) else str(val),
        })

    # Period-over-period change
    for i in range(1, len(trend)):
        prev = Decimal(trend[i - 1]["value"])
        curr = Decimal(trend[i]["value"])
        if prev != 0:
            change = ((curr - prev) / prev * Decimal("100")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
            trend[i]["change_pct"] = f"{change}%"
        else:
            trend[i]["change_pct"] = "N/A"

    ok({
        "metric": metric,
        "period": {"from_date": from_date, "to_date": to_date},
        "periodicity": periodicity,
        "trend": trend,
    })


# ===========================================================================
# ACTION: period-comparison
# ===========================================================================

def action_period_comparison(conn, args):
    """Compare metrics across custom periods."""
    _require_company(args)

    periods_json = getattr(args, "periods", None)
    if not periods_json:
        err('--periods is required (JSON array of {"from_date":"...","to_date":"...","label":"..."})')

    periods = _parse_json_arg(periods_json, "periods")
    if not isinstance(periods, list) or len(periods) < 2:
        err("--periods must be a JSON array with at least 2 period objects")

    metrics_json = getattr(args, "metrics", None)
    if metrics_json:
        requested_metrics = _parse_json_arg(metrics_json, "metrics")
    else:
        requested_metrics = ["revenue", "expenses", "net_income"]

    company_id = args.company_id
    modules = _check_modules(conn)

    columns = []
    for p in periods:
        if "from_date" not in p or "to_date" not in p:
            err("Each period must have from_date and to_date")
        label = p.get("label", f"{p['from_date']} to {p['to_date']}")

        col = {"label": label, "from_date": p["from_date"], "to_date": p["to_date"]}

        for metric in requested_metrics:
            if metric == "revenue":
                col["revenue"] = _s(_get_account_balance(
                    conn, company_id, root_type="income",
                    from_date=p["from_date"], to_date=p["to_date"]
                ))
            elif metric == "expenses":
                col["expenses"] = _s(_get_account_balance(
                    conn, company_id, root_type="expense",
                    from_date=p["from_date"], to_date=p["to_date"]
                ))
            elif metric == "net_income":
                rev = _get_account_balance(conn, company_id, root_type="income",
                                           from_date=p["from_date"], to_date=p["to_date"])
                exp = _get_account_balance(conn, company_id, root_type="expense",
                                           from_date=p["from_date"], to_date=p["to_date"])
                col["net_income"] = _s(rev - exp)
            elif metric == "headcount":
                if modules.get("erpclaw-hr"):
                    cnt = conn.execute(
                        "SELECT COUNT(*) as cnt FROM employee WHERE company_id = ? AND status = 'active' AND date_of_joining <= ?",
                        (company_id, p["to_date"]),
                    ).fetchone()["cnt"]
                    col["headcount"] = cnt
                else:
                    col["headcount"] = None

        columns.append(col)

    # Compute variance between consecutive periods
    if len(columns) >= 2:
        for metric in requested_metrics:
            for i in range(1, len(columns)):
                prev_val = columns[i - 1].get(metric)
                curr_val = columns[i].get(metric)
                if prev_val is not None and curr_val is not None:
                    try:
                        p = Decimal(str(prev_val))
                        c = Decimal(str(curr_val))
                        columns[i][f"{metric}_change"] = _s(c - p)
                        if p != 0:
                            pct = ((c - p) / p * Decimal("100")).quantize(Decimal("0.1"))
                            columns[i][f"{metric}_change_pct"] = f"{pct}%"
                    except Exception:
                        pass

    ok({
        "metrics": requested_metrics,
        "periods": columns,
    })


# ===========================================================================
# S35: Query Performance Audit
# ===========================================================================

# Critical queries that represent the most common patterns across all skills.
# Each entry: (name, sql, params) — params may be placeholders.
_PERF_QUERIES = [
    ("gl_entry_by_company", "SELECT * FROM gl_entry WHERE company_id = ?", ["x"]),
    ("gl_entry_by_voucher", "SELECT * FROM gl_entry WHERE voucher_type = ? AND voucher_no = ?", ["x", "x"]),
    ("gl_entry_by_account_date", "SELECT * FROM gl_entry WHERE account_id = ? AND posting_date BETWEEN ? AND ?", ["x", "2026-01-01", "2026-12-31"]),
    ("gl_aggregate_by_account", "SELECT account_id, SUM(CAST(debit AS REAL)) as d, SUM(CAST(credit AS REAL)) as c FROM gl_entry WHERE company_id = ? AND posting_date <= ? GROUP BY account_id", ["x", "2026-12-31"]),
    ("account_by_company", "SELECT * FROM account WHERE company_id = ?", ["x"]),
    ("account_by_root_type", "SELECT * FROM account WHERE company_id = ? AND root_type = ?", ["x", "Asset"]),
    ("sales_invoice_list", "SELECT * FROM sales_invoice WHERE company_id = ? AND docstatus = ? ORDER BY posting_date DESC LIMIT 50", ["x", "1"]),
    ("purchase_invoice_list", "SELECT * FROM purchase_invoice WHERE company_id = ? AND docstatus = ? ORDER BY posting_date DESC LIMIT 50", ["x", "1"]),
    ("stock_ledger_by_item", "SELECT * FROM stock_ledger_entry WHERE item_id = ? ORDER BY posting_date DESC", ["x"]),
    ("stock_balance_by_warehouse", "SELECT item_id, SUM(CAST(qty_change AS REAL)) as qty FROM stock_ledger_entry WHERE warehouse_id = ? GROUP BY item_id", ["x"]),
    ("customer_list", "SELECT * FROM customer WHERE company_id = ? ORDER BY name LIMIT 50", ["x"]),
    ("supplier_list", "SELECT * FROM supplier WHERE company_id = ? ORDER BY name LIMIT 50", ["x"]),
    ("payment_entry_list", "SELECT * FROM payment_entry WHERE company_id = ? AND docstatus = ? ORDER BY posting_date DESC LIMIT 50", ["x", "1"]),
    ("journal_entry_list", "SELECT * FROM journal_entry WHERE company_id = ? AND docstatus = ? ORDER BY posting_date DESC LIMIT 50", ["x", "1"]),
    ("employee_list", "SELECT * FROM employee WHERE company_id = ? AND status = ? ORDER BY full_name LIMIT 50", ["x", "Active"]),
    ("item_list", "SELECT * FROM item WHERE company_id = ? ORDER BY item_code LIMIT 50", ["x"]),
    ("audit_log_recent", "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 100", []),
    ("payment_ledger_outstanding", "SELECT * FROM payment_ledger_entry WHERE party_type = ? AND outstanding_amount != '0' ORDER BY due_date", ["Customer"]),
]


def action_analyze_query_performance(conn, args):
    """Analyze query performance across all ERPClaw tables.

    Runs EXPLAIN QUERY PLAN on critical queries and reports:
    - Full table scans (SCAN TABLE without index)
    - Index usage vs. full scans
    - Recommendations for missing indexes
    """
    results = []
    full_scans = []
    index_used = 0
    total_queries = 0

    for name, sql, params in _PERF_QUERIES:
        # Check if required tables exist
        # Extract table names from SQL
        skip = False
        for table_name in ["sales_invoice", "purchase_invoice", "stock_ledger_entry",
                           "customer", "supplier", "payment_entry", "journal_entry",
                           "employee", "item", "payment_ledger_entry"]:
            if table_name in sql and not table_exists(conn, table_name):
                skip = True
                break
        if skip:
            continue

        try:
            explain_sql = f"EXPLAIN QUERY PLAN {sql}"
            rows = conn.execute(explain_sql, params).fetchall()
            total_queries += 1

            plan_steps = []
            uses_index = False
            is_scan = False
            for row in rows:
                detail = row["detail"] if isinstance(row, sqlite3.Row) else row[3]
                plan_steps.append(detail)
                if "USING INDEX" in detail or "USING COVERING INDEX" in detail:
                    uses_index = True
                if "SCAN TABLE" in detail and "USING" not in detail:
                    is_scan = True

            if uses_index:
                index_used += 1

            entry = {
                "query": name,
                "plan": plan_steps,
                "uses_index": uses_index,
                "full_scan": is_scan,
            }
            results.append(entry)

            if is_scan:
                full_scans.append(name)

        except sqlite3.OperationalError:
            continue  # Table might not exist

    # Index coverage stats
    tables_result = conn.execute(
        "SELECT COUNT(*) as cnt FROM sqlite_master WHERE type='index'"
    ).fetchone()
    total_indexes = tables_result["cnt"] if isinstance(tables_result, sqlite3.Row) else tables_result[0]

    table_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM sqlite_master WHERE type='table'"
    ).fetchone()
    total_tables = table_count["cnt"] if isinstance(table_count, sqlite3.Row) else table_count[0]

    # Recommendations
    recommendations = []
    if full_scans:
        recommendations.append(f"{len(full_scans)} queries use full table scans: {', '.join(full_scans)}")
    if total_queries > 0:
        pct = round(index_used / total_queries * 100, 1)
        if pct < 80:
            recommendations.append(f"Index utilization is {pct}% — consider adding indexes for frequently queried columns")

    ok({
        "total_queries_analyzed": total_queries,
        "queries_using_index": index_used,
        "full_table_scans": len(full_scans),
        "index_utilization_pct": round(index_used / max(total_queries, 1) * 100, 1),
        "total_indexes": total_indexes,
        "total_tables": total_tables,
        "full_scan_queries": full_scans,
        "query_plans": results,
        "recommendations": recommendations,
    })


# ===========================================================================
# Action dispatch
# ===========================================================================

ACTIONS = {
    # Utility
    "status": action_status,
    "available-metrics": action_available_metrics,
    "analyze-query-performance": action_analyze_query_performance,
    # Financial ratios
    "liquidity-ratios": action_liquidity_ratios,
    "profitability-ratios": action_profitability_ratios,
    "efficiency-ratios": action_efficiency_ratios,
    # Revenue
    "revenue-by-customer": action_revenue_by_customer,
    "revenue-by-item": action_revenue_by_item,
    "revenue-trend": action_revenue_trend,
    "customer-concentration": action_customer_concentration,
    # Expenses
    "expense-breakdown": action_expense_breakdown,
    "cost-trend": action_cost_trend,
    "opex-vs-capex": action_opex_vs_capex,
    # Inventory
    "abc-analysis": action_abc_analysis,
    "inventory-turnover": action_inventory_turnover,
    "aging-inventory": action_aging_inventory,
    # HR
    "headcount-analytics": action_headcount_analytics,
    "payroll-analytics": action_payroll_analytics,
    "leave-utilization": action_leave_utilization,
    # Operations
    "project-profitability": action_project_profitability,
    "quality-dashboard": action_quality_dashboard,
    "support-metrics": action_support_metrics,
    # Dashboards & trends
    "executive-dashboard": action_executive_dashboard,
    "company-scorecard": action_company_scorecard,
    "metric-trend": action_metric_trend,
    "period-comparison": action_period_comparison,
}


def main():
    parser = argparse.ArgumentParser(description="ERPClaw Analytics")
    parser.add_argument("--action", required=True, help="Action to execute")
    parser.add_argument("--db-path", default=None, help="Path to SQLite database")
    # Common
    parser.add_argument("--company-id", dest="company_id")
    parser.add_argument("--from-date", dest="from_date")
    parser.add_argument("--to-date", dest="to_date")
    parser.add_argument("--as-of-date", dest="as_of_date")
    parser.add_argument("--account-id", dest="account_id")
    parser.add_argument("--cost-center-id", dest="cost_center_id")
    parser.add_argument("--project-id", dest="project_id")
    # Pagination
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")
    # Analytics-specific
    parser.add_argument("--periodicity", default="monthly")
    parser.add_argument("--group-by", dest="group_by", default="account")
    parser.add_argument("--metric", default=None)
    parser.add_argument("--periods", default=None, help="JSON array of periods")
    parser.add_argument("--metrics", default=None, help="JSON array of metric names")
    parser.add_argument("--aging-buckets", dest="aging_buckets", default="30,60,90,120")
    # Inventory
    parser.add_argument("--item-id", dest="item_id")
    parser.add_argument("--warehouse-id", dest="warehouse_id")
    # HR
    parser.add_argument("--department-id", dest="department_id")

    args, _unknown = parser.parse_known_args()
    check_input_lengths(args)

    action_fn = ACTIONS.get(args.action)
    if not action_fn:
        err(f"Unknown action: {args.action}. Available: {', '.join(sorted(ACTIONS.keys()))}")

    db_path = args.db_path or DEFAULT_DB_PATH
    ensure_db_exists(db_path)
    conn = get_connection(db_path)

    # Hard requirement: setup + gl must be installed
    dep = check_required_tables(conn, ["company", "account", "gl_entry"])
    if dep:
        print(json.dumps(dep, indent=2))
        sys.exit(1)

    try:
        action_fn(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
