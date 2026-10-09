#!/usr/bin/env python3
"""ERPClaw Reports Skill — db_query.py

Read-only financial reporting. Owns NO tables — reads gl_entry,
payment_ledger_entry, account, budget, fiscal_year, etc.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import csv
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from contextlib import redirect_stdout
from decimal import Decimal, InvalidOperation

# Add shared lib to path
try:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.decimal_utils import to_decimal, round_currency
    from erpclaw_lib.validation import check_input_lengths
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables, table_exists
    from erpclaw_lib.query_helpers import resolve_company_id, resolve_scope_company
    from erpclaw_lib.voucher_types import canonical_voucher_type
    # Aliased: this module already has a `party_ledger` ACTION function (the
    # `party-ledger` report at :1049), and a bare import would be shadowed by it.
    from erpclaw_lib import party_ledger as party_ledger_rules
    from erpclaw_lib.query import Q, P, Table, Field, Case, fn, DecimalSum, DecimalAbs, json_get
    from erpclaw_lib.rule_evaluation import evaluate_rule
    from erpclaw_lib.vendor.pypika import Order
    from erpclaw_lib.vendor.pypika.terms import LiteralValue
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw first: clawhub install erpclaw", "suggestion": "clawhub install erpclaw"}))
    sys.exit(1)


REQUIRED_TABLES = ["company", "account", "gl_entry"]

# Closing vouchers zero income/expense into retained earnings. They are a
# bookkeeping transfer, not activity, so the P&L-shaped reports exclude them.
_CLOSING_VOUCHER_TYPE = "period_closing"


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


def _parse_json_arg(value, name):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        err(f"Invalid JSON for --{name}: {value}")


def _dimension_filter(args, alias=None):
    """Build an accounting-dimension WHERE fragment (M6) from repeated
    ``--dimension-key/--dimension-value`` pairs.

    Returns ``(" AND <frag> = ? [AND ...]", [values])`` for raw-SQL concatenation,
    or ``("", [])`` when no dimension filter was requested. The key is rendered via
    ``erpclaw_lib.query.json_get`` (dialect-aware + ANSI-escaped); the value is a
    bound parameter, never interpolated. ``alias`` qualifies the JSON column when
    the query aliases ``gl_entry`` (e.g. ``"g"``); pass ``None`` for unaliased SQL.
    """
    keys = getattr(args, "dimension_key", None) or []
    vals = getattr(args, "dimension_value", None) or []
    if len(keys) != len(vals):
        err("Each --dimension-key must be paired with a --dimension-value")
    if not keys:
        return "", []
    col = f"{alias}.dimensions_json" if alias else "dimensions_json"
    clauses, params = [], []
    for k, v in zip(keys, vals):
        clauses.append(f"{json_get(col, k)} = ?")
        params.append(v)
    return " AND " + " AND ".join(clauses), params


def _assert_dimension_registered(conn, key):
    """M6: reject a --group-by/--dimension key that is not an active registered
    accounting dimension, with a message that points at `list-dimensions`.

    A key absent from dimension_registry (or present but deactivated) means the
    user asked to group by something the books never tag, so the grouped report
    would be empty/meaningless; failing loudly beats a silently-empty statement.
    """
    row = conn.execute(
        "SELECT is_active FROM dimension_registry WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        err(f"Unknown accounting dimension '{key}'. Register it with "
            f"add-dimension, or run list-dimensions to see the available "
            f"dimensions to group by.")
    if not row["is_active"]:
        err(f"Accounting dimension '{key}' is deactivated and cannot be used "
            f"to group a report. Run list-dimensions to see active dimensions.")


# ---------------------------------------------------------------------------
# Trial Balance
# ---------------------------------------------------------------------------

def trial_balance(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.to_date:
        err("--to-date is required")

    from_date = args.from_date
    to_date = args.to_date
    project_id = getattr(args, "project_id", None)

    # Build optional project filter clause and params
    proj_clause = ""
    proj_params = ()
    if project_id:
        proj_clause = " AND project_id = ?"
        proj_params = (project_id,)

    # Multi-dimensional filter (M6): repeated --dimension-key/--dimension-value.
    _dim_clause, _dim_params = _dimension_filter(args, alias=None)
    proj_clause += _dim_clause
    proj_params = proj_params + tuple(_dim_params)

    # Get all accounts for the company
    acct_t = Table("account")
    sql = (
        Q.from_(acct_t)
        .select(
            acct_t.id, acct_t.name, acct_t.account_number,
            acct_t.root_type, acct_t.account_type, acct_t.is_group,
        )
        .where(acct_t.company_id == P())
        .orderby(acct_t.account_number)
        .orderby(acct_t.name)
        .get_sql()
    )
    accounts = conn.execute(sql, (company_id,)).fetchall()

    result = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")

    gl_t = Table("gl_entry")

    for acct in accounts:
        if acct["is_group"]:
            continue

        aid = acct["id"]

        # Opening balance (before from_date, or all time if no from_date)
        if from_date:
            # Raw SQL: COALESCE(decimal_sum(...), '0') with aliased columns kept for clarity
            opening = conn.execute(
                """SELECT COALESCE(decimal_sum(debit), '0') as d,
                          COALESCE(decimal_sum(credit), '0') as c
                   FROM gl_entry WHERE account_id = ? AND posting_date < ?
                   AND is_cancelled = 0""" + proj_clause,
                (aid, from_date) + proj_params,
            ).fetchone()
        else:
            opening = {"d": 0, "c": 0}

        # Period movement
        if from_date:
            period = conn.execute(
                """SELECT COALESCE(decimal_sum(debit), '0') as d,
                          COALESCE(decimal_sum(credit), '0') as c
                   FROM gl_entry WHERE account_id = ?
                   AND posting_date >= ? AND posting_date <= ?
                   AND is_cancelled = 0""" + proj_clause,
                (aid, from_date, to_date) + proj_params,
            ).fetchone()
        else:
            period = conn.execute(
                """SELECT COALESCE(decimal_sum(debit), '0') as d,
                          COALESCE(decimal_sum(credit), '0') as c
                   FROM gl_entry WHERE account_id = ?
                   AND posting_date <= ? AND is_cancelled = 0""" + proj_clause,
                (aid, to_date) + proj_params,
            ).fetchone()

        op_d = _d(opening["d"])
        op_c = _d(opening["c"])
        per_d = _d(period["d"])
        per_c = _d(period["c"])
        cl_d = op_d + per_d
        cl_c = op_c + per_c

        # Skip accounts with zero activity
        if cl_d == 0 and cl_c == 0:
            continue

        total_debit += cl_d
        total_credit += cl_c

        result.append({
            "account_id": aid,
            "account_name": acct["name"],
            "account_number": acct["account_number"] or "",
            "root_type": acct["root_type"],
            "opening_debit": _s(op_d),
            "opening_credit": _s(op_c),
            "debit": _s(per_d),
            "credit": _s(per_c),
            "closing_debit": _s(cl_d),
            "closing_credit": _s(cl_c),
        })

    ok({
        "as_of_date": to_date,
        "total_debit": _s(total_debit),
        "total_credit": _s(total_credit),
        "accounts": result,
    })


# ---------------------------------------------------------------------------
# Profit & Loss
# ---------------------------------------------------------------------------

_UNTAGGED_BUCKET = "(untagged)"


def _grouped_pl(conn, company_id, key, from_date, to_date, dim_clause, dim_params):
    """Compose a P&L broken down by one accounting dimension value (M6).

    income/expense accounts ONLY (root_type filter), netted per value of `key`
    read from gl_entry.dimensions_json via the shared dialect-aware json_get
    helper — the same grouping idiom multi_dim_trial_balance uses, scoped here to
    the P&L account types. Entries whose dimensions_json lacks `key` collapse to
    a single ``(untagged)`` bucket (COALESCE on the json_get fragment) so nothing
    is dropped. Returns (groups, income_total, expense_total) with exact Decimals.
    """
    frag = str(json_get("g.dimensions_json", key))  # dialect-aware, key-escaped
    # The grouped expression is repeated (SELECT + GROUP BY) because PG disallows a
    # SELECT-list alias in GROUP BY; COALESCE folds NULL/absent keys into one bucket.
    bucket_expr = f"COALESCE({frag}, ?)"
    # decimal_sum returns TEXT on both backends; keep the exact subtraction in Python.
    sql = (
        "SELECT " + bucket_expr + " AS dim_value, a.root_type AS root_type, "
        "COALESCE(decimal_sum(g.debit), '0') AS total_debit, "
        "COALESCE(decimal_sum(g.credit), '0') AS total_credit "
        "FROM account a "
        "LEFT JOIN gl_entry g ON g.account_id = a.id "
        "  AND g.posting_date >= ? AND g.posting_date <= ? "
        "  AND g.is_cancelled = 0" + dim_clause + " AND g.voucher_type <> ? "
        "WHERE a.company_id = ? AND a.root_type IN ('income', 'expense') "
        "  AND a.is_group = 0 "
        "GROUP BY " + bucket_expr + ", a.root_type "
        "ORDER BY dim_value"
    )
    # Param order mirrors the textual ?-order: SELECT bucket default, JOIN dates +
    # dim filter + closing-type exclusion, WHERE company, GROUP BY bucket default.
    params = ([_UNTAGGED_BUCKET, from_date, to_date]
              + list(dim_params) + [_CLOSING_VOUCHER_TYPE, company_id,
                                    _UNTAGGED_BUCKET])
    rows = conn.execute(sql, params).fetchall()

    # Fold the (value, root_type) rows into per-value {revenue, expenses}.
    acc = {}
    for r in rows:
        val = r["dim_value"]
        d = _d(r["total_debit"])
        c = _d(r["total_credit"])
        if d == 0 and c == 0:
            continue  # LEFT JOIN produced an all-account row with no activity
        slot = acc.setdefault(val, {"revenue": Decimal("0"), "expenses": Decimal("0")})
        if r["root_type"] == "income":
            slot["revenue"] += (c - d)
        else:  # expense
            slot["expenses"] += (d - c)

    groups, income_total, expense_total = [], Decimal("0"), Decimal("0")
    for val in sorted(acc.keys(), key=lambda v: (v == _UNTAGGED_BUCKET, str(v))):
        rev = acc[val]["revenue"]
        exp = acc[val]["expenses"]
        income_total += rev
        expense_total += exp
        groups.append({
            key: val,
            "revenue": _s(rev),
            "expenses": _s(exp),
            "net": _s(rev - exp),
        })
    return groups, income_total, expense_total


def profit_and_loss(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.from_date:
        err("--from-date is required")
    if not args.to_date:
        err("--to-date is required")

    # M6 routing: "P&L grouped by <dimension>" is handled HERE (the natural call
    # the agent reaches for) by delegating to the shared M6 grouping helper, rather
    # than forcing the agent to discover multi-dim-trial-balance. Absent --group-by,
    # the flat company-wide statement below is byte-identical to before.
    group_by_raw = (getattr(args, "group_by", None) or "").strip()
    if group_by_raw:
        keys = [k.strip() for k in group_by_raw.split(",") if k.strip()]
        if not keys:
            err('--group-by needs a dimension name (e.g. --group-by department); '
                'run list-dimensions to see the available dimensions.')
        if len(keys) != 1:
            err('--group-by takes exactly one dimension for a P&L breakdown '
                '(e.g. --group-by department); use multi-dim-trial-balance for '
                'multi-dimension grouping.')
        key = keys[0]
        _assert_dimension_registered(conn, key)
        # An optional --dimension-key/--dimension-value filter scopes the subset
        # first; then we break that subset down by `key` (filter-then-group).
        _dim_clause, _dim_params = _dimension_filter(args, alias="g")
        groups, income_total, expense_total = _grouped_pl(
            conn, company_id, key, args.from_date, args.to_date,
            _dim_clause, _dim_params)
        ok({
            "period": f"{args.from_date} to {args.to_date}",
            "group_by": key,
            "groups": groups,
            "income_total": _s(income_total),
            "expense_total": _s(expense_total),
            "net_income": _s(income_total - expense_total),
        })

    project_id = getattr(args, "project_id", None)
    proj_join_clause = ""
    proj_params = ()
    if project_id:
        proj_join_clause = " AND g.project_id = ?"
        proj_params = (project_id,)

    # Multi-dimensional filter (M6): folded into the LEFT JOIN ON like project_id.
    _dim_clause, _dim_params = _dimension_filter(args, alias="g")
    proj_join_clause += _dim_clause
    proj_params = proj_params + tuple(_dim_params)

    # Raw SQL: too complex for PyPika, readability preserved
    # (LEFT JOIN with date range in ON clause). Each leg is fetched as text
    # with the exact-decimal sum helper and subtracted in Python with
    # Decimal: decimal_sum returns TEXT on both backends, and subtracting
    # (or casting) the text sums inside the SQL goes through a binary float
    # on SQLite and is rejected on PostgreSQL. Exact-zero nets are dropped
    # in Python, so there is no HAVING for PG to reject.
    income_rows = conn.execute(
        f"""SELECT a.id, a.name, a.account_number,
                  COALESCE(decimal_sum(g.credit), '0') as total_credit,
                  COALESCE(decimal_sum(g.debit), '0') as total_debit
           FROM account a
           LEFT JOIN gl_entry g ON g.account_id = a.id
               AND g.posting_date >= ? AND g.posting_date <= ?
               AND g.is_cancelled = 0 AND g.voucher_type <> ?{proj_join_clause}
           WHERE a.company_id = ? AND a.root_type = 'income' AND a.is_group = 0
           GROUP BY a.id
           ORDER BY a.account_number, a.name""",
        (args.from_date, args.to_date, _CLOSING_VOUCHER_TYPE) + proj_params + (company_id,),
    ).fetchall()

    # Raw SQL: too complex for PyPika, readability preserved
    expense_rows = conn.execute(
        f"""SELECT a.id, a.name, a.account_number,
                  COALESCE(decimal_sum(g.debit), '0') as total_debit,
                  COALESCE(decimal_sum(g.credit), '0') as total_credit
           FROM account a
           LEFT JOIN gl_entry g ON g.account_id = a.id
               AND g.posting_date >= ? AND g.posting_date <= ?
               AND g.is_cancelled = 0 AND g.voucher_type <> ?{proj_join_clause}
           WHERE a.company_id = ? AND a.root_type = 'expense' AND a.is_group = 0
           GROUP BY a.id
           ORDER BY a.account_number, a.name""",
        (args.from_date, args.to_date, _CLOSING_VOUCHER_TYPE) + proj_params + (company_id,),
    ).fetchall()

    income = []
    income_total = Decimal("0")
    for r in income_rows:
        amt = _d(r["total_credit"]) - _d(r["total_debit"])
        if amt == 0:
            continue
        income.append({"account": r["name"], "account_id": r["id"],
                       "amount": _s(amt)})
        income_total += amt
    expenses = []
    expense_total = Decimal("0")
    for r in expense_rows:
        amt = _d(r["total_debit"]) - _d(r["total_credit"])
        if amt == 0:
            continue
        expenses.append({"account": r["name"], "account_id": r["id"],
                         "amount": _s(amt)})
        expense_total += amt
    net_income = income_total - expense_total

    ok({
        "period": f"{args.from_date} to {args.to_date}",
        "income": income,
        "income_total": _s(income_total),
        "expenses": expenses,
        "expense_total": _s(expense_total),
        "net_income": _s(net_income),
    })


# ---------------------------------------------------------------------------
# Balance Sheet
# ---------------------------------------------------------------------------

def balance_sheet(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.as_of_date:
        err("--as-of-date is required")

    project_id = getattr(args, "project_id", None)
    proj_join_clause = ""
    proj_where_clause = ""
    proj_join_params = ()
    proj_where_params = ()
    if project_id:
        proj_join_clause = " AND g.project_id = ?"
        proj_where_clause = " AND g.project_id = ?"
        proj_join_params = (project_id,)
        proj_where_params = (project_id,)

    # Multi-dimensional filter (M6): applied to both the section LEFT JOIN ON and
    # the net-income gl_entry WHERE so the statement still balances.
    _dim_clause, _dim_params = _dimension_filter(args, alias="g")
    proj_join_clause += _dim_clause
    proj_where_clause += _dim_clause
    proj_join_params = proj_join_params + tuple(_dim_params)
    proj_where_params = proj_where_params + tuple(_dim_params)

    def _section(root_type, debit_positive=True):
        # Raw SQL: too complex for PyPika, readability preserved
        # (LEFT JOIN with date filter in ON clause; no HAVING clause)
        # SELECT stays TEXT (decimal_sum's native return) so Python keeps doing
        # the exact-Decimal subtraction in _section. The Python loop below
        # already drops every account whose amount is zero, so no
        # database-side zero-row filter is needed.
        rows = conn.execute(
            """SELECT a.id, a.name, a.account_number,
                      COALESCE(decimal_sum(g.debit), '0') as total_debit,
                      COALESCE(decimal_sum(g.credit), '0') as total_credit
               FROM account a
               LEFT JOIN gl_entry g ON g.account_id = a.id
                   AND g.posting_date <= ? AND g.is_cancelled = 0""" + proj_join_clause + """
               WHERE a.company_id = ? AND a.root_type = ? AND a.is_group = 0
               GROUP BY a.id
               ORDER BY a.account_number, a.name""",
            (args.as_of_date,) + proj_join_params + (company_id, root_type),
        ).fetchall()

        items = []
        total = Decimal("0")
        for r in rows:
            d = _d(r["total_debit"])
            c = _d(r["total_credit"])
            amt = (d - c) if debit_positive else (c - d)
            if amt == 0:
                continue
            items.append({"account": r["name"], "account_id": r["id"],
                          "amount": _s(amt)})
            total += amt
        return items, total

    assets, total_assets = _section("asset", debit_positive=True)
    liabilities, total_liabilities = _section("liability", debit_positive=False)
    equity_items, total_equity_base = _section("equity", debit_positive=False)

    # Calculate current year net income for equity section
    # Get the fiscal year start for the as_of_date
    fy_t = Table("fiscal_year")
    fy_sql = (
        Q.from_(fy_t)
        .select(fy_t.start_date)
        .where(fy_t.company_id == P())
        .where(fy_t.start_date <= P())
        .where(fy_t.end_date >= P())
        .orderby(fy_t.start_date, order=Order.desc)
        .limit(1)
        .get_sql()
    )
    fy = conn.execute(fy_sql, (company_id, args.as_of_date, args.as_of_date)).fetchone()

    net_income_ytd = Decimal("0")
    if fy:
        fy_start = fy["start_date"]
        # Exact-decimal year-to-date totals: each leg is fetched as text with
        # the exact-decimal sum helper (text '0' when there are no rows) and
        # the legs are subtracted in Python with Decimal, so no binary float
        # ever touches money. The optional project/dimension filters travel
        # as bound parameters, exactly as before.
        g_t = Table("gl_entry").as_("g")
        a_t = Table("account").as_("a")
        inc_q = (
            Q.from_(g_t)
            .join(a_t).on(a_t.id == g_t.account_id)
            .select(
                fn.Coalesce(DecimalSum(g_t.credit), "0").as_("total_credit"),
                fn.Coalesce(DecimalSum(g_t.debit), "0").as_("total_debit"),
            )
            .where(a_t.company_id == P())
            .where(a_t.root_type == "income")
            .where(g_t.posting_date >= P())
            .where(g_t.posting_date <= P())
            .where(g_t.is_cancelled == 0)
        )
        inc = conn.execute(
            inc_q.get_sql() + proj_where_clause,
            (company_id, fy_start, args.as_of_date) + proj_where_params,
        ).fetchone()
        exp_q = (
            Q.from_(g_t)
            .join(a_t).on(a_t.id == g_t.account_id)
            .select(
                fn.Coalesce(DecimalSum(g_t.debit), "0").as_("total_debit"),
                fn.Coalesce(DecimalSum(g_t.credit), "0").as_("total_credit"),
            )
            .where(a_t.company_id == P())
            .where(a_t.root_type == "expense")
            .where(g_t.posting_date >= P())
            .where(g_t.posting_date <= P())
            .where(g_t.is_cancelled == 0)
        )
        exp = conn.execute(
            exp_q.get_sql() + proj_where_clause,
            (company_id, fy_start, args.as_of_date) + proj_where_params,
        ).fetchone()
        income_total = _d(inc["total_credit"]) - _d(inc["total_debit"])
        expense_total = _d(exp["total_debit"]) - _d(exp["total_credit"])
        net_income_ytd = income_total - expense_total

    total_equity = total_equity_base + net_income_ytd

    ok({
        "as_of_date": args.as_of_date,
        "assets": assets,
        "total_assets": _s(total_assets),
        "liabilities": liabilities,
        "total_liabilities": _s(total_liabilities),
        "equity": equity_items,
        "total_equity": _s(total_equity),
        "net_income_ytd": _s(net_income_ytd),
    })


# ---------------------------------------------------------------------------
# Cash Flow (indirect method)
# ---------------------------------------------------------------------------

def cash_flow(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.from_date:
        err("--from-date is required")
    if not args.to_date:
        err("--to-date is required")

    # Multi-dimensional filter (M6): repeated --dimension-key/--dimension-value.
    _dim_clause, _dim_params = _dimension_filter(args, alias="g")
    _dim_params = tuple(_dim_params)

    # Raw SQL: too complex for PyPika, readability preserved
    # (JOIN + decimal_sum arithmetic in SELECT with IN clause on account_type)
    opening = conn.execute(
        """SELECT COALESCE(decimal_sum(g.debit), '0') as total_debit,
                  COALESCE(decimal_sum(g.credit), '0') as total_credit
           FROM gl_entry g JOIN account a ON a.id = g.account_id
           WHERE a.company_id = ? AND a.account_type IN ('bank','cash')
           AND g.posting_date < ? AND g.is_cancelled = 0""" + _dim_clause,
        (company_id, args.from_date) + _dim_params,
    ).fetchone()
    opening_balance = _d(opening["total_debit"]) - _d(opening["total_credit"])

    # Raw SQL: too complex for PyPika, readability preserved
    closing = conn.execute(
        """SELECT COALESCE(decimal_sum(g.debit), '0') as total_debit,
                  COALESCE(decimal_sum(g.credit), '0') as total_credit
           FROM gl_entry g JOIN account a ON a.id = g.account_id
           WHERE a.company_id = ? AND a.account_type IN ('bank','cash')
           AND g.posting_date <= ? AND g.is_cancelled = 0""" + _dim_clause,
        (company_id, args.to_date) + _dim_params,
    ).fetchone()
    closing_balance = _d(closing["total_debit"]) - _d(closing["total_credit"])

    net_change = closing_balance - opening_balance

    # Simplified: categorize by account type
    # Operating: income/expense + current asset/liability changes
    # Investing: fixed asset changes
    # Financing: equity + loan changes
    details = []

    # Raw SQL: too complex for PyPika, readability preserved
    # (JOIN + decimal_sum + NOT IN clause). The legs stay TEXT aliases read
    # in Python; HAVING repeats the aggregates wrapped in
    # CAST(... AS NUMERIC) because PG disallows a SELECT-list alias there.
    movements = conn.execute(
        """SELECT a.id, a.name, a.root_type, a.account_type,
                  COALESCE(decimal_sum(g.debit), '0') as d,
                  COALESCE(decimal_sum(g.credit), '0') as c
           FROM gl_entry g JOIN account a ON a.id = g.account_id
           WHERE a.company_id = ?
           AND g.posting_date >= ? AND g.posting_date <= ?
           AND g.is_cancelled = 0
           AND (a.account_type IS NULL OR a.account_type NOT IN ('bank','cash'))""" + _dim_clause + """
           GROUP BY a.id
           HAVING CAST(COALESCE(decimal_sum(g.debit), '0') AS NUMERIC) != 0
               OR CAST(COALESCE(decimal_sum(g.credit), '0') AS NUMERIC) != 0
           ORDER BY a.root_type, a.name""",
        (company_id, args.from_date, args.to_date) + _dim_params,
    ).fetchall()

    operating = Decimal("0")
    investing = Decimal("0")
    financing = Decimal("0")

    for m in movements:
        d = _d(m["d"])
        c = _d(m["c"])
        root = m["root_type"]
        atype = m["account_type"] or ""

        if root == "income":
            amt = c - d  # Income increases cash
            operating += amt
            cat = "operating"
        elif root == "expense":
            amt = -(d - c)  # Expenses decrease cash
            operating += amt
            cat = "operating"
        elif root == "asset" and atype in ("fixed_asset", "accumulated_depreciation"):
            amt = -(d - c)
            investing += amt
            cat = "investing"
        elif root == "asset":
            amt = -(d - c)  # Increase in current asset = cash outflow
            operating += amt
            cat = "operating"
        elif root == "liability":
            amt = c - d  # Increase in liability = cash inflow
            if atype in ("Long Term Loan",):
                financing += amt
                cat = "financing"
            else:
                operating += amt
                cat = "operating"
        elif root == "equity":
            amt = c - d
            financing += amt
            cat = "financing"
        else:
            amt = c - d
            operating += amt
            cat = "operating"

        if amt != 0:
            details.append({
                "category": cat,
                "account": m["name"],
                "amount": _s(amt),
            })

    ok({
        "operating": _s(operating),
        "investing": _s(investing),
        "financing": _s(financing),
        "net_change": _s(net_change),
        "opening_balance": _s(opening_balance),
        "closing_balance": _s(closing_balance),
        "details": details,
    })


# ---------------------------------------------------------------------------
# General Ledger
# ---------------------------------------------------------------------------

def general_ledger(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.from_date:
        err("--from-date is required")
    if not args.to_date:
        err("--to-date is required")

    limit = int(args.limit or "100")
    offset = int(args.offset or "0")

    # Multi-dimensional filter (M6): repeated --dimension-key/--dimension-value,
    # rendered as a single AND-joined literal (keys via json_get, values bound).
    _dim_clause, _dim_params = _dimension_filter(args, alias="g")
    _dim_lit = _dim_clause[5:] if _dim_clause else ""  # strip leading " AND "

    gl_t = Table("gl_entry").as_("g")
    acct_t = Table("account").as_("a")

    # Build opening balance query dynamically
    opening_q = (
        Q.from_(gl_t)
        .join(acct_t).on(acct_t.id == gl_t.account_id)
        .select(
            fn.Coalesce(DecimalSum(gl_t.debit), "0").as_("total_debit"),
            fn.Coalesce(DecimalSum(gl_t.credit), "0").as_("total_credit"),
        )
        .where(gl_t.posting_date < P())
        .where(gl_t.is_cancelled == 0)
        .where(acct_t.company_id == P())
    )
    opening_params = [args.from_date, company_id]

    if args.account_id:
        opening_q = opening_q.where(gl_t.account_id == P())
        opening_params.append(args.account_id)
    if _dim_lit:
        opening_q = opening_q.where(LiteralValue(_dim_lit))
        opening_params.extend(_dim_params)

    opening = conn.execute(opening_q.get_sql(), opening_params).fetchone()
    opening_balance = _d(opening["total_debit"]) - _d(opening["total_credit"])

    # Build period entries query dynamically
    entries_q = (
        Q.from_(gl_t)
        .join(acct_t).on(acct_t.id == gl_t.account_id)
        .select(gl_t.star, acct_t.name.as_("account_name"))
        .where(gl_t.posting_date >= P())
        .where(gl_t.posting_date <= P())
        .where(gl_t.is_cancelled == 0)
        .where(acct_t.company_id == P())
    )
    entries_params = [args.from_date, args.to_date, company_id]

    if args.account_id:
        entries_q = entries_q.where(gl_t.account_id == P())
        entries_params.append(args.account_id)
    if args.party_type:
        entries_q = entries_q.where(gl_t.party_type == P())
        entries_params.append(args.party_type)
    if args.party_id:
        entries_q = entries_q.where(gl_t.party_id == P())
        entries_params.append(args.party_id)
    if args.voucher_type:
        # FINDING-006: a label filter ("Sales Invoice") should match stored
        # "sales_invoice" gl_entry rows.
        entries_q = entries_q.where(gl_t.voucher_type == P())
        entries_params.append(canonical_voucher_type(args.voucher_type))
    if _dim_lit:
        entries_q = entries_q.where(LiteralValue(_dim_lit))
        entries_params.extend(_dim_params)

    entries_q = (
        entries_q
        .orderby(gl_t.posting_date)
        .orderby(gl_t.created_at)
        .limit(P())
        .offset(P())
    )
    entries_params += [limit, offset]

    entries = conn.execute(entries_q.get_sql(), entries_params).fetchall()

    total_debit = Decimal("0")
    total_credit = Decimal("0")
    running_balance = opening_balance
    result = []

    for e in entries:
        d = _d(e["debit"])
        c = _d(e["credit"])
        total_debit += d
        total_credit += c
        running_balance += (d - c)

        result.append({
            "posting_date": e["posting_date"],
            "account_name": e["account_name"],
            "debit": _s(d),
            "credit": _s(c),
            "balance": _s(running_balance),
            "voucher_type": e["voucher_type"],
            "voucher_id": e["voucher_id"],
            "party_type": e["party_type"] or "",
            "party_id": e["party_id"] or "",
            "remarks": e["remarks"] or "",
        })

    ok({
        "entries": result,
        "opening_balance": _s(opening_balance),
        "total_debit": _s(total_debit),
        "total_credit": _s(total_credit),
        "closing_balance": _s(running_balance),
    })


# ---------------------------------------------------------------------------
# AR/AP Aging
# ---------------------------------------------------------------------------

_PARTY_TABLE_ALLOWLIST = {"customer": "customer", "supplier": "supplier"}

def _aging_report(conn, args, party_type_label, party_table, party_name_col="name"):
    """AR/AP aging from the party payment ledger.

    Reads the party ledger through the CANONICAL rules
    (``erpclaw_lib.party_ledger``, ADR-0032 Decision 2) — reader disposition R1.
    Both queries below used to filter a flat ``delinked = 0``, which is wrong in
    two directions at once: it drops a payment's delinked original while keeping
    its active cancel mirror, so every aging figure was wrong after a
    ``cancel-payment`` (measured: 1,600.00 where the truth is 1,000.00), and it
    kept a cancelled invoice's own row alive nowhere. Payment rows are now netted
    reversal-inclusive; document rows still require ``delinked = 0``.

    The values this report returns changed with Wave G F2 (M38): the party-level
    double-count is compensated in the ledger itself, so a 1,000.00 invoice paid
    300.00 now ages 700.00 rather than 400.00. M139 adopts the ATTRIBUTION half
    of the canon too (previously only get-outstanding did): each bucket's
    residual is aged at its own document's date, so that same invoice paid in
    May reads 700.00 in the invoice's bucket in June, not 1,000.00 there and
    -300.00 in current. Buckets attributed to a payment (unapplied cash,
    residual compensation) are reported as `unapplied`, never aged. A released
    allocation (an invoice cancelled while cash was applied) legitimately shows
    as a negative `unapplied` amount.
    """
    if party_table not in _PARTY_TABLE_ALLOWLIST:
        err(f"Invalid party table: {party_table}")
    company_id = resolve_scope_company(conn,
                                       getattr(args, 'company_id', None),
                                       getattr(args, 'company_name', None))
    if not args.as_of_date:
        err("--as-of-date is required")

    buckets_str = args.aging_buckets or "30,60,90,120"
    try:
        buckets = [int(b) for b in buckets_str.split(",")]
    except ValueError:
        err("Invalid --aging-buckets format (expected comma-separated integers)")

    # Get outstanding by party from payment_ledger_entry
    ple_t = Table("payment_ledger_entry")
    acct_t = Table("account")
    acct_scope_sub = (
        Q.from_(acct_t)
        .select(acct_t.id)
        .where(acct_t.company_id == P())
    )
    outstanding_sql = (
        Q.from_(ple_t)
        .select(
            ple_t.party_id,
            DecimalSum(ple_t.amount).as_("total"),
            fn.Min(ple_t.posting_date).as_("earliest_date"),
        )
        .where(ple_t.party_type == P())
        .where(party_ledger_rules.live_rows_criterion())
        .where(ple_t.posting_date <= P())
        .where(ple_t.account_id.isin(acct_scope_sub))
        .groupby(ple_t.party_id)
        .having(
            # Repeat the aggregate rather than the "total" SELECT alias: PostgreSQL
            # disallows output-column aliases in HAVING, and CAST(... AS NUMERIC)
            # replaces the SQLite-only "text + 0" numeric coercion.
            LiteralValue(
                "CAST(decimal_sum(amount) AS NUMERIC) > 0.005 "
                "OR CAST(decimal_sum(amount) AS NUMERIC) < -0.005"
            )
        )
        .get_sql()
    )
    outstanding = conn.execute(outstanding_sql, (party_type_label, args.as_of_date, company_id)).fetchall()

    # Residuals per attributed bucket — the get_outstanding attribution canon
    # (ADR-0032 Decision 2, correction C6): live rows grouped by
    # (party, bucket voucher) and summed, so a payment's allocation rows reduce
    # the invoice they point at. The age date of a bucket is the posting date
    # of the bucket's own document row
    # (MIN(CASE WHEN own voucher = bucket THEN posting_date END)), falling back
    # to the bucket's earliest row when no such row exists (a payment applied
    # to an invoice that posted later). Buckets whose residual is zero are
    # dropped by the HAVING, as get_outstanding does.
    bucket_type = party_ledger_rules.bucket_voucher_type_term()
    bucket_id = party_ledger_rules.bucket_voucher_id_term()
    doc_date_case = Case().when(
        (ple_t.voucher_type == bucket_type) & (ple_t.voucher_id == bucket_id),
        ple_t.posting_date,
    )
    buckets_sql = (
        Q.from_(ple_t)
        .select(
            ple_t.party_id,
            bucket_type.as_("bucket_type"),
            bucket_id.as_("bucket_id"),
            DecimalSum(ple_t.amount).as_("residual"),
            fn.Min(doc_date_case).as_("doc_date"),
            fn.Min(ple_t.posting_date).as_("min_date"),
        )
        .where(ple_t.party_type == P())
        .where(party_ledger_rules.live_rows_criterion())
        .where(ple_t.posting_date <= P())
        .where(ple_t.account_id.isin(acct_scope_sub))
        .groupby(ple_t.party_id, bucket_type, bucket_id)
        .having(LiteralValue('CAST(decimal_sum("amount") AS NUMERIC) != 0'))
        .get_sql()
    )
    bucket_rows = conn.execute(buckets_sql, (party_type_label, args.as_of_date, company_id)).fetchall()

    buckets_by_party = {}
    for row in bucket_rows:
        pid = row["party_id"]
        if pid not in buckets_by_party:
            buckets_by_party[pid] = []
        buckets_by_party[pid].append(row)

    result = []
    total_outstanding = Decimal("0")

    for o in outstanding:
        pid = o["party_id"]
        if party_type_label == "customer":
            cust_t = Table("customer")
            party_sql = (
                Q.from_(cust_t)
                .select(cust_t.id, cust_t.name.as_("pname"))
                .where(cust_t.id == P())
                .get_sql()
            )
            party = conn.execute(party_sql, (pid,)).fetchone()
        else:
            supp_t = Table("supplier")
            party_sql = (
                Q.from_(supp_t)
                .select(supp_t.id, supp_t.name.as_("pname"))
                .where(supp_t.id == P())
                .get_sql()
            )
            party = conn.execute(party_sql, (pid,)).fetchone()
        pname = party["pname"] if party else pid

        # Age each attributed bucket's RESIDUAL at its own document's age.
        # A bucket attributed to a payment (unapplied cash, residual
        # compensation) is not an invoice: it is reported as `unapplied` and
        # never aged into the day buckets.
        as_of = datetime.strptime(args.as_of_date, "%Y-%m-%d")
        bucket_amounts = [Decimal("0")] * (len(buckets) + 1)  # +1 for beyond last bucket
        unapplied = Decimal("0")

        for brow in buckets_by_party.get(pid, []):
            residual = _d(brow["residual"])
            if round_currency(residual) == Decimal("0"):
                continue
            if brow["bucket_type"] == "payment_entry":
                unapplied += residual
                continue
            age_date = brow["doc_date"] or brow["min_date"]
            days = (as_of - datetime.strptime(age_date, "%Y-%m-%d")).days

            placed = False
            for i, b in enumerate(buckets):
                if i == 0 and days <= b:
                    bucket_amounts[0] += residual
                    placed = True
                    break
                elif i > 0 and days > buckets[i-1] and days <= b:
                    bucket_amounts[i] += residual
                    placed = True
                    break
            if not placed:
                bucket_amounts[-1] += residual

        party_total = _d(o["total"])
        total_outstanding += party_total

        if abs(party_total - (sum(bucket_amounts) + unapplied)) > Decimal("0.005"):
            err(f"Aging buckets plus unapplied do not sum to party total "
                f"for {pid}: {party_total} != {sum(bucket_amounts)} + {unapplied}")

        entry = {
            f"{party_type_label}_id": pid,
            f"{party_type_label}_name": pname,
            "current": _s(bucket_amounts[0]),
        }
        for i, b in enumerate(buckets):
            if i == 0:
                entry["current"] = _s(bucket_amounts[0])
            else:
                entry[f"days_{b}"] = _s(bucket_amounts[i])
        if len(buckets) > 1:
            entry[f"days_{buckets[0]}"] = _s(bucket_amounts[0])
            for i in range(1, len(buckets)):
                entry[f"days_{buckets[i]}"] = _s(bucket_amounts[i])
        entry[f"days_{buckets[-1]}_plus"] = _s(bucket_amounts[-1])
        entry["unapplied"] = _s(unapplied)
        entry["total"] = _s(party_total)
        result.append(entry)

    ok({
        "as_of_date": args.as_of_date,
        "total_outstanding": _s(total_outstanding),
        f"{party_type_label}s": result,
    })


def ar_aging(conn, args):
    _aging_report(conn, args, "customer", "customer")


def ap_aging(conn, args):
    _aging_report(conn, args, "supplier", "supplier")


# ---------------------------------------------------------------------------
# Shared budget/actual basis + flux variance narrative (v1)
# ---------------------------------------------------------------------------

def _budget_actual_amounts(conn, fy, budget_row):
    """Shared budget and actual basis for the budget variance reports.

    Single source of the ledger calculation behind both `budget-vs-actual`
    (alias `budget-variance`) and `flux-variance-narrative`: the stored
    budget amount plus the fiscal-year actuals as signed debit-minus-credit
    over non-cancelled GL entries. Callers differ only in presentation
    (percent nullability, thresholds, narrative), never in these amounts.
    """
    budget_amt = _d(budget_row["budget_amount"])

    gl_t = Table("gl_entry").as_("g")
    actual_q = (
        Q.from_(gl_t)
        .select(
            fn.Coalesce(DecimalSum(gl_t.debit), "0").as_("total_debit"),
            fn.Coalesce(DecimalSum(gl_t.credit), "0").as_("total_credit"),
        )
        .where(gl_t.is_cancelled == 0)
        .where(gl_t.posting_date >= P())
        .where(gl_t.posting_date <= P())
    )
    actual_params = [fy["start_date"], fy["end_date"]]

    if budget_row["account_id"]:
        actual_q = actual_q.where(gl_t.account_id == P())
        actual_params.append(budget_row["account_id"])
    if budget_row["cost_center_id"]:
        actual_q = actual_q.where(gl_t.cost_center_id == P())
        actual_params.append(budget_row["cost_center_id"])

    actual = conn.execute(actual_q.get_sql(), actual_params).fetchone()
    actual_amt = _d(actual["total_debit"]) - _d(actual["total_credit"])
    return budget_amt, actual_amt


def _parse_materiality(value, name):
    """Parse an optional exact-decimal threshold; refuse invalid/negative."""
    raw = value if value is not None else "0.00"
    if isinstance(raw, float):
        err(f"Invalid Decimal for {name}: {raw!r}")
    if isinstance(raw, Decimal):
        parsed = raw
    else:
        try:
            parsed = to_decimal(str(raw))
        except (ValueError, TypeError, InvalidOperation):
            err(f"Invalid Decimal for {name}: {raw!r}")
    if not parsed.is_finite():
        err(f"Invalid Decimal for {name}: {raw!r}")
    if parsed < 0:
        err(f"{name} must be nonnegative")
    return parsed


def flux_variance_narrative(conn, args):
    """Deterministic plain-language reading of one budget variance row.

    Read-only: only SELECTs, then `ok()`. Reuses `_budget_actual_amounts`,
    the exact basis behind `budget-variance`, and adds an exact Decimal
    variance percent (null when the budget is zero), caller-threshold
    classification and a traceable narrative. No model call, no stored
    narrative, no writes.
    """
    if not getattr(args, "fiscal_year_id", None):
        err("--fiscal-year-id is required")
    if not getattr(args, "company_id", None) and not getattr(
            args, "company_name", None):
        err("--company-id is required")
    if not getattr(args, "account_id", None):
        err("--account-id is required")
    company_id = resolve_company_id(conn,
                                    getattr(args, "company_id", None),
                                    getattr(args, "company_name", None))

    mat_amt = _parse_materiality(
        getattr(args, "materiality_amount", None), "--materiality-amount")
    mat_pct = _parse_materiality(
        getattr(args, "materiality_percent", None), "--materiality-percent")

    fy_t = Table("fiscal_year")
    fy = conn.execute(
        Q.from_(fy_t).select(fy_t.star).where(fy_t.id == P()).get_sql(),
        (args.fiscal_year_id,)).fetchone()
    if not fy:
        err(f"Fiscal year not found: {args.fiscal_year_id}")
    if fy["company_id"] != company_id:
        err(f"Fiscal year {args.fiscal_year_id} does not belong "
            f"to company {company_id}")

    acct_t = Table("account")
    acct = conn.execute(
        Q.from_(acct_t).select(acct_t.star).where(acct_t.id == P()).get_sql(),
        (args.account_id,)).fetchone()
    if not acct:
        err(f"Account not found: {args.account_id}")
    if acct["company_id"] != company_id:
        err(f"Account {args.account_id} does not belong "
            f"to company {company_id}")

    b_t = Table("budget")
    brow = conn.execute(
        Q.from_(b_t).select(b_t.star)
        .where(b_t.fiscal_year_id == P())
        .where(b_t.company_id == P())
        .where(b_t.account_id == P()).get_sql(),
        (args.fiscal_year_id, company_id, args.account_id)).fetchone()
    if not brow:
        err(f"Budget not found for account {args.account_id} "
            f"in fiscal year {args.fiscal_year_id}")

    budget_amt, actual_amt = _budget_actual_amounts(conn, fy, brow)
    variance = budget_amt - actual_amt
    if budget_amt == 0:
        pct = None
    else:
        pct = variance / budget_amt * 100

    budget_s = _s(budget_amt)
    actual_s = _s(actual_amt)
    variance_s = _s(variance)
    pct_s = None if pct is None else _s(pct)

    material = abs(variance) >= mat_amt or (
        pct is not None and abs(pct) >= mat_pct)
    classification = "material" if material else "within_threshold"
    direction = "unfavorable" if variance < 0 else "favorable"

    account_name = acct["name"]
    if pct_s is None:
        narrative = (
            "Account '%s': budget %s, actual %s, variance %s; "
            "no percentage comparison is available (budget is zero); "
            "direction is %s; classification: %s."
            % (account_name, budget_s, actual_s, variance_s,
               direction, classification)
        )
    else:
        narrative = (
            "Account '%s': budget %s, actual %s, variance %s (%s%%) is %s; "
            "classification: %s."
            % (account_name, budget_s, actual_s, variance_s,
               pct_s, direction, classification)
        )

    ok({
        "account": account_name,
        "account_id": args.account_id,
        "company_id": company_id,
        "fiscal_year_id": args.fiscal_year_id,
        "budget": budget_s,
        "actual": actual_s,
        "variance": variance_s,
        "variance_pct": pct_s,
        "variance_percent": pct_s,
        "direction": direction,
        "classification": classification,
        "narrative": narrative,
        "basis": {
            "company_id": company_id,
            "fiscal_year_id": args.fiscal_year_id,
            "account_id": args.account_id,
            "account": account_name,
            "materiality_amount": _s(mat_amt),
            "materiality_percent": _s(mat_pct),
            "report_action": "budget-variance",
        },
    })


# ---------------------------------------------------------------------------
# Budget vs Actual
# ---------------------------------------------------------------------------

def budget_vs_actual(conn, args):
    if not args.fiscal_year_id:
        err("--fiscal-year-id is required")
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    fy_t = Table("fiscal_year")
    fy_sql = (
        Q.from_(fy_t)
        .select(fy_t.star)
        .where(fy_t.id == P())
        .get_sql()
    )
    fy = conn.execute(fy_sql, (args.fiscal_year_id,)).fetchone()
    if not fy:
        err(f"Fiscal year not found: {args.fiscal_year_id}")

    # Build budgets query dynamically
    b_t = Table("budget").as_("b")
    acct_t = Table("account").as_("a")
    cc_t = Table("cost_center").as_("cc")

    budgets_q = (
        Q.from_(b_t)
        .left_join(acct_t).on(acct_t.id == b_t.account_id)
        .left_join(cc_t).on(cc_t.id == b_t.cost_center_id)
        .select(b_t.star, acct_t.name.as_("account_name"), cc_t.name.as_("cc_name"))
        .where(b_t.fiscal_year_id == P())
        .where(b_t.company_id == P())
    )
    budgets_params = [args.fiscal_year_id, company_id]

    if args.account_id:
        budgets_q = budgets_q.where(b_t.account_id == P())
        budgets_params.append(args.account_id)
    if args.cost_center_id:
        budgets_q = budgets_q.where(b_t.cost_center_id == P())
        budgets_params.append(args.cost_center_id)

    budgets_q = budgets_q.orderby(acct_t.name)
    budgets = conn.execute(budgets_q.get_sql(), budgets_params).fetchall()

    items = []
    for b in budgets:
        budget_amt, actual_amt = _budget_actual_amounts(conn, fy, b)

        variance = budget_amt - actual_amt
        variance_pct = (variance / budget_amt * 100) if budget_amt else Decimal("0")

        items.append({
            "account_or_cc": b["account_name"] or b["cc_name"] or "Unknown",
            "budget": _s(budget_amt),
            "actual": _s(actual_amt),
            "variance": _s(variance),
            "variance_pct": _s(variance_pct),
            "action_if_exceeded": b["action_if_exceeded"],
        })

    ok({"items": items})


# ---------------------------------------------------------------------------
# Party Ledger
# ---------------------------------------------------------------------------

def party_ledger(conn, args):
    if not args.party_type or args.party_type not in ("customer", "supplier", "employee"):
        err("--party-type must be 'customer', 'supplier' or 'employee'")
    if not args.party_id:
        err("--party-id is required")

    if args.party_type == "customer":
        cust_t = Table("customer")
        party_sql = (
            Q.from_(cust_t)
            .select(cust_t.name, cust_t.company_id)
            .where(cust_t.id == P())
            .get_sql()
        )
        party = conn.execute(party_sql, (args.party_id,)).fetchone()
    elif args.party_type == "supplier":
        supp_t = Table("supplier")
        party_sql = (
            Q.from_(supp_t)
            .select(supp_t.name, supp_t.company_id)
            .where(supp_t.id == P())
            .get_sql()
        )
        party = conn.execute(party_sql, (args.party_id,)).fetchone()
    elif args.party_type == "employee":
        emp_t = Table("employee")
        party_sql = (
            Q.from_(emp_t)
            .select(emp_t.full_name, emp_t.company_id)
            .where(emp_t.id == P())
            .get_sql()
        )
        party = conn.execute(party_sql, (args.party_id,)).fetchone()
    else:
        err("--party-type must be 'customer', 'supplier' or 'employee'")
    if not party:
        if args.party_type == "customer":
            err(f"Customer {args.party_id} not found")
        if args.party_type == "supplier":
            err(f"Supplier {args.party_id} not found")
        err(f"Employee {args.party_id} not found")
    party_name = party["full_name"] if args.party_type == "employee" else party["name"]
    # The party anchors the scope: with no company the read covers the
    # party's own company's rows; a given company must exist and a party
    # of another company is refused.
    if getattr(args, "company_id", None) or getattr(args, "company_name", None):
        scope_company_id = resolve_scope_company(
            conn, getattr(args, "company_id", None),
            getattr(args, "company_name", None))
        if party["company_id"] != scope_company_id:
            if args.party_type == "customer":
                err(f"Customer {args.party_id} belongs to another company")
            if args.party_type == "supplier":
                err(f"Supplier {args.party_id} belongs to another company")
            err(f"Employee {args.party_id} belongs to another company")
    else:
        scope_company_id = party["company_id"]

    # A party may also be tagged on the balancing cash/bank leg of a
    # payment. A party ledger is the control-account ledger, so include only
    # receivable rows for customers, payable rows for suppliers, and both
    # payable and payroll-payable rows for employees. Otherwise a customer
    # invoice for 500 followed by a 200 payment can incorrectly remain at 500
    # when both payment legs carry the party, or an employee ledger can omit
    # net pay posted to Payroll Payable.
    if args.party_type == "customer":
        party_account_types = ("receivable",)
    elif args.party_type == "employee":
        party_account_types = ("payable", "payroll_payable")
    else:
        party_account_types = ("payable",)

    gl_t = Table("gl_entry").as_("g")
    acct_t = Table("account")
    acct_scope_sub = (
        Q.from_(acct_t)
        .select(acct_t.id)
        .where(acct_t.company_id == P())
        .where(acct_t.account_type.isin([P() for _ in party_account_types]))
    )

    # Opening balance (before from_date)
    if args.from_date:
        opening_q = (
            Q.from_(gl_t)
            .select(
                fn.Coalesce(DecimalSum(gl_t.debit), "0").as_("total_debit"),
                fn.Coalesce(DecimalSum(gl_t.credit), "0").as_("total_credit"),
            )
            .where(gl_t.party_type == P())
            .where(gl_t.party_id == P())
            .where(gl_t.is_cancelled == 0)
            .where(gl_t.posting_date < P())
            .where(gl_t.account_id.isin(acct_scope_sub))
        )
        opening_params = [args.party_type, args.party_id, args.from_date,
                          scope_company_id] + list(party_account_types)
    else:
        # No from_date → no opening balance (1=0 condition)
        opening_q = (
            Q.from_(gl_t)
            .select(
                fn.Coalesce(DecimalSum(gl_t.debit), "0").as_("total_debit"),
                fn.Coalesce(DecimalSum(gl_t.credit), "0").as_("total_credit"),
            )
            .where(gl_t.party_type == P())
            .where(gl_t.party_id == P())
            .where(gl_t.is_cancelled == 0)
            .where(LiteralValue("1 = 0"))
        )
        opening_params = [args.party_type, args.party_id]

    opening = conn.execute(opening_q.get_sql(), opening_params).fetchone()
    opening_balance = _d(opening["total_debit"]) - _d(opening["total_credit"])

    # Period entries
    entries_q = (
        Q.from_(gl_t)
        .select(gl_t.posting_date, gl_t.voucher_type, gl_t.voucher_id, gl_t.debit, gl_t.credit)
        .where(gl_t.party_type == P())
        .where(gl_t.party_id == P())
        .where(gl_t.is_cancelled == 0)
        .where(gl_t.account_id.isin(acct_scope_sub))
    )
    entries_params = [
        args.party_type, args.party_id, scope_company_id,
    ] + list(party_account_types)

    if args.from_date:
        entries_q = entries_q.where(gl_t.posting_date >= P())
        entries_params.append(args.from_date)
    if args.to_date:
        entries_q = entries_q.where(gl_t.posting_date <= P())
        entries_params.append(args.to_date)

    entries_q = entries_q.orderby(gl_t.posting_date).orderby(gl_t.created_at)
    entries = conn.execute(entries_q.get_sql(), entries_params).fetchall()

    running = opening_balance
    result = []
    for e in entries:
        d = _d(e["debit"])
        c = _d(e["credit"])
        running += (d - c)
        result.append({
            "posting_date": e["posting_date"],
            "voucher_type": e["voucher_type"],
            "voucher_id": e["voucher_id"],
            "debit": _s(d),
            "credit": _s(c),
            "balance": _s(running),
        })

    ok({
        "party_name": party_name,
        "opening_balance": _s(opening_balance),
        "entries": result,
        "closing_balance": _s(running),
    })


# ---------------------------------------------------------------------------
# Tax Summary
# ---------------------------------------------------------------------------

def tax_summary(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.from_date:
        err("--from-date is required")
    if not args.to_date:
        err("--to-date is required")

    # Tax accounts are those of type "Tax Payable" or similar
    # Raw SQL: too complex for PyPika, readability preserved
    # (LEFT JOIN with date range in ON clause, decimal_sum aggregates aliased in SELECT)
    tax_accounts = conn.execute(
        """SELECT a.id, a.name, a.account_type,
                  COALESCE(decimal_sum(g.credit), '0') as collected,
                  COALESCE(decimal_sum(g.debit), '0') as paid
           FROM account a
           LEFT JOIN gl_entry g ON g.account_id = a.id
               AND g.posting_date >= ? AND g.posting_date <= ?
               AND g.is_cancelled = 0
           WHERE a.company_id = ?
           AND a.account_type = 'tax'
           AND a.is_group = 0
           GROUP BY a.id
           ORDER BY a.name""",
        (args.from_date, args.to_date, company_id),
    ).fetchall()

    total_collected = Decimal("0")
    total_paid = Decimal("0")
    by_account = []

    for ta in tax_accounts:
        collected = _d(ta["collected"])
        paid = _d(ta["paid"])
        net = collected - paid
        if net == 0 and collected == 0 and paid == 0:
            continue
        total_collected += collected
        total_paid += paid
        by_account.append({
            "account_id": ta["id"],
            "account_name": ta["name"],
            "amount": _s(net),
        })

    ok({
        "collected": _s(total_collected),
        "paid": _s(total_paid),
        "net_liability": _s(total_collected - total_paid),
        "by_account": by_account,
    })


# ---------------------------------------------------------------------------
# Payment Summary
# ---------------------------------------------------------------------------

def payment_summary(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.from_date:
        err("--from-date is required")
    if not args.to_date:
        err("--to-date is required")

    pe_t = Table("payment_entry")

    received_sql = (
        Q.from_(pe_t)
        .select(fn.Coalesce(DecimalSum(pe_t.paid_amount), "0").as_("total"))
        .where(pe_t.company_id == P())
        .where(pe_t.status == "submitted")
        .where(pe_t.payment_type == "receive")
        .where(pe_t.posting_date >= P())
        .where(pe_t.posting_date <= P())
        .get_sql()
    )
    received = conn.execute(received_sql, (company_id, args.from_date, args.to_date)).fetchone()

    paid_sql = (
        Q.from_(pe_t)
        .select(fn.Coalesce(DecimalSum(pe_t.paid_amount), "0").as_("total"))
        .where(pe_t.company_id == P())
        .where(pe_t.status == "submitted")
        .where(pe_t.payment_type == "pay")
        .where(pe_t.posting_date >= P())
        .where(pe_t.posting_date <= P())
        .get_sql()
    )
    paid = conn.execute(paid_sql, (company_id, args.from_date, args.to_date)).fetchone()

    by_party_sql = (
        Q.from_(pe_t)
        .select(
            pe_t.party_type,
            fn.Count("*").as_("cnt"),
            fn.Coalesce(DecimalSum(pe_t.paid_amount), "0").as_("amount"),
        )
        .where(pe_t.company_id == P())
        .where(pe_t.status == "submitted")
        .where(pe_t.posting_date >= P())
        .where(pe_t.posting_date <= P())
        .groupby(pe_t.party_type)
        .get_sql()
    )
    by_party = conn.execute(by_party_sql, (company_id, args.from_date, args.to_date)).fetchall()

    ok({
        "total_received": _s(_d(received["total"])),
        "total_paid": _s(_d(paid["total"])),
        "by_party_type": [
            {"party_type": r["party_type"] or "unknown",
             "count": r["cnt"], "amount": _s(_d(r["amount"]))}
            for r in by_party
        ],
    })


# ---------------------------------------------------------------------------
# GL Summary
# ---------------------------------------------------------------------------

def gl_summary(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.from_date:
        err("--from-date is required")
    if not args.to_date:
        err("--to-date is required")

    gl_t = Table("gl_entry").as_("g")
    acct_t = Table("account").as_("a")

    sql = (
        Q.from_(gl_t)
        .join(acct_t).on(acct_t.id == gl_t.account_id)
        .select(
            gl_t.voucher_type,
            fn.Count("*").as_("cnt"),
            fn.Coalesce(DecimalSum(gl_t.debit), "0").as_("total_debit"),
            fn.Coalesce(DecimalSum(gl_t.credit), "0").as_("total_credit"),
        )
        .where(acct_t.company_id == P())
        .where(gl_t.posting_date >= P())
        .where(gl_t.posting_date <= P())
        .where(gl_t.is_cancelled == 0)
        .groupby(gl_t.voucher_type)
        .orderby(gl_t.voucher_type)
        .get_sql()
    )
    rows = conn.execute(sql, (company_id, args.from_date, args.to_date)).fetchall()

    ok({
        "by_voucher_type": [
            {"voucher_type": r["voucher_type"],
             "count": r["cnt"],
             "total_debit": _s(_d(r["total_debit"])),
             "total_credit": _s(_d(r["total_credit"]))}
            for r in rows
        ],
    })


# ---------------------------------------------------------------------------
# Comparative P&L
# ---------------------------------------------------------------------------

def comparative_pl(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    periods = _parse_json_arg(args.periods, "periods")
    if not periods or not isinstance(periods, list):
        err("--periods must be a non-empty JSON array of {from_date, to_date, label}")

    # Get all income/expense accounts
    acct_t = Table("account")
    accts_sql = (
        Q.from_(acct_t)
        .select(acct_t.id, acct_t.name, acct_t.root_type)
        .where(acct_t.company_id == P())
        .where(acct_t.root_type.isin(["income", "expense"]))
        .where(acct_t.is_group == 0)
        .orderby(acct_t.root_type)
        .orderby(acct_t.name)
        .get_sql()
    )
    accounts = conn.execute(accts_sql, (company_id,)).fetchall()

    result_accounts = []
    totals = []

    gl_t = Table("gl_entry")

    for period in periods:
        fd = period.get("from_date")
        td = period.get("to_date")
        label = period.get("label", f"{fd} to {td}")
        p_income = Decimal("0")
        p_expense = Decimal("0")

        for acct in accounts:
            if acct["root_type"] == "income":
                row = conn.execute(
                    """SELECT COALESCE(decimal_sum(credit), '0') as total_credit,
                              COALESCE(decimal_sum(debit), '0') as total_debit
                       FROM gl_entry WHERE account_id = ?
                       AND posting_date >= ? AND posting_date <= ?
                       AND is_cancelled = 0 AND voucher_type <> ?""",
                    (acct["id"], fd, td, _CLOSING_VOUCHER_TYPE),
                ).fetchone()
                amt = _d(row["total_credit"]) - _d(row["total_debit"])
                p_income += amt
            else:
                row = conn.execute(
                    """SELECT COALESCE(decimal_sum(debit), '0') as total_debit,
                              COALESCE(decimal_sum(credit), '0') as total_credit
                       FROM gl_entry WHERE account_id = ?
                       AND posting_date >= ? AND posting_date <= ?
                       AND is_cancelled = 0 AND voucher_type <> ?""",
                    (acct["id"], fd, td, _CLOSING_VOUCHER_TYPE),
                ).fetchone()
                amt = _d(row["total_debit"]) - _d(row["total_credit"])
                p_expense += amt

            # Find or create account entry in result
            existing = None
            for ra in result_accounts:
                if ra["account_id"] == acct["id"]:
                    existing = ra
                    break
            if not existing:
                existing = {"account": acct["name"], "account_id": acct["id"],
                            "root_type": acct["root_type"], "periods": []}
                result_accounts.append(existing)
            existing["periods"].append({"label": label, "amount": _s(amt)})

        totals.append({
            "label": label,
            "income": _s(p_income),
            "expenses": _s(p_expense),
            "net": _s(p_income - p_expense),
        })

    ok({"accounts": result_accounts, "totals": totals})


# ---------------------------------------------------------------------------
# Weekly Digest
# ---------------------------------------------------------------------------

_WEEK_LEN_DAYS = 7

_SALES_STATUSES = ["submitted", "partially_paid", "paid", "overdue"]
_OVERDUE_STATUSES = ["submitted", "partially_paid", "overdue"]
_OPEN_ORDER_STATUSES = ["draft", "confirmed", "partially_delivered",
                        "fully_delivered", "partially_invoiced"]


def _unavailable(table):
    return {"status": "unavailable",
            "reason": "Table '%s' is not installed; section unavailable" % table}


def weekly_digest(conn, args):
    """Deterministic read-only seven-day business digest for one company.

    Requires ``--company-id`` (or ``--company``) and ``--start-date``
    YYYY-MM-DD; the inclusive end date is exactly start + 6 days. Reports
    exact Decimal sales (submitted sales invoices posted in the window),
    collections (submitted receive payments in the window), spending
    (submitted purchase bills posted in the window), overdue receivables
    (open submitted invoices due before the week end), and point-in-time
    open sales/purchase order counts. Every query is company scoped.
    A missing optional table yields a section-level ``unavailable`` result,
    never an invented zero. Only SELECTs; deterministic key order, no
    timestamps, so two identical calls return identical JSON.
    """
    if not getattr(args, "company_id", None) and not getattr(
            args, "company_name", None):
        err("--company-id is required")
    company_id = resolve_company_id(conn,
                                    getattr(args, "company_id", None),
                                    getattr(args, "company_name", None))

    comp_t = Table("company")
    found = conn.execute(
        Q.from_(comp_t).select(comp_t.id)
        .where(comp_t.id == P()).get_sql(),
        (company_id,)).fetchone()
    if not found:
        err("Company not found: %s" % company_id)

    raw_start = getattr(args, "start_date", None) or getattr(
        args, "from_date", None)
    if not raw_start:
        err("--start-date is required")
    try:
        start_dt = datetime.strptime(raw_start, "%Y-%m-%d")
        if start_dt.strftime("%Y-%m-%d") != raw_start:
            raise ValueError()
    except (TypeError, ValueError):
        err("Invalid --start-date '%s': expected YYYY-MM-DD" % raw_start)
    week_start = start_dt.strftime("%Y-%m-%d")
    week_end = (start_dt + timedelta(days=_WEEK_LEN_DAYS - 1)).strftime(
        "%Y-%m-%d")

    sections = ["sales", "collections", "spending",
                "overdue_receivables", "operations"]

    if table_exists(conn, "sales_invoice"):
        si_t = Table("sales_invoice")
        sales_sql = (
            Q.from_(si_t)
            .select(fn.Count("*").as_("cnt"),
                    fn.Coalesce(DecimalSum(si_t.grand_total), "0")
                    .as_("total"))
            .where(si_t.company_id == P())
            .where(si_t.posting_date >= P())
            .where(si_t.posting_date <= P())
            .where(si_t.status.isin(_SALES_STATUSES))
            .get_sql()
        )
        sales_row = conn.execute(
            sales_sql, (company_id, week_start, week_end)).fetchone()
        sales = {"status": "available",
                 "invoice_count": sales_row["cnt"],
                 "total": _s(_d(sales_row["total"]))}
    else:
        sales = _unavailable("sales_invoice")

    if table_exists(conn, "payment_entry"):
        pe_t = Table("payment_entry")
        coll_sql = (
            Q.from_(pe_t)
            .select(fn.Count("*").as_("cnt"),
                    fn.Coalesce(DecimalSum(pe_t.paid_amount), "0")
                    .as_("total"))
            .where(pe_t.company_id == P())
            .where(pe_t.status == "submitted")
            .where(pe_t.payment_type == "receive")
            .where(pe_t.posting_date >= P())
            .where(pe_t.posting_date <= P())
            .get_sql()
        )
        coll_row = conn.execute(
            coll_sql, (company_id, week_start, week_end)).fetchone()
        collections = {"status": "available",
                       "receipt_count": coll_row["cnt"],
                       "total": _s(_d(coll_row["total"]))}
    else:
        collections = _unavailable("payment_entry")

    if table_exists(conn, "purchase_invoice"):
        pi_t = Table("purchase_invoice")
        spend_sql = (
            Q.from_(pi_t)
            .select(fn.Count("*").as_("cnt"),
                    fn.Coalesce(DecimalSum(pi_t.grand_total), "0")
                    .as_("total"))
            .where(pi_t.company_id == P())
            .where(pi_t.posting_date >= P())
            .where(pi_t.posting_date <= P())
            .where(pi_t.status.isin(_SALES_STATUSES))
            .get_sql()
        )
        spend_row = conn.execute(
            spend_sql, (company_id, week_start, week_end)).fetchone()
        spending = {"status": "available",
                    "bill_count": spend_row["cnt"],
                    "total": _s(_d(spend_row["total"]))}
    else:
        spending = _unavailable("purchase_invoice")

    if table_exists(conn, "sales_invoice"):
        od_t = Table("sales_invoice")
        od_sql = (
            Q.from_(od_t)
            .select(od_t.id, od_t.grand_total,
                    od_t.outstanding_amount, od_t.due_date)
            .where(od_t.company_id == P())
            .where(od_t.status.isin(_OVERDUE_STATUSES))
            .where(od_t.due_date < P())
            .orderby(od_t.due_date)
            .orderby(od_t.id)
            .get_sql()
        )
        od_rows = conn.execute(od_sql, (company_id, week_end)).fetchall()
        end_dt = datetime.strptime(week_end, "%Y-%m-%d")
        overdue_total = Decimal("0")
        overdue_invoices = []
        for row in od_rows:
            outstanding = _d(row["outstanding_amount"])
            if outstanding <= 0:
                continue
            due_dt = datetime.strptime(row["due_date"], "%Y-%m-%d")
            overdue_total += outstanding
            overdue_invoices.append({
                "id": row["id"],
                "due_date": row["due_date"],
                "days_overdue": (end_dt - due_dt).days,
                "grand_total": _s(_d(row["grand_total"])),
                "outstanding": _s(outstanding),
            })
        overdue = {"status": "available",
                   "overdue_count": len(overdue_invoices),
                   "total_overdue": _s(overdue_total),
                   "invoices": overdue_invoices}
    else:
        overdue = _unavailable("sales_invoice")

    ops_missing = []
    open_so = None
    open_po = None
    if table_exists(conn, "sales_order"):
        so_t = Table("sales_order")
        so_sql = (
            Q.from_(so_t)
            .select(fn.Count("*").as_("cnt"))
            .where(so_t.company_id == P())
            .where(so_t.status.isin(_OPEN_ORDER_STATUSES))
            .get_sql()
        )
        open_so = conn.execute(so_sql, (company_id,)).fetchone()["cnt"]
    else:
        ops_missing.append("sales_order")
    if table_exists(conn, "purchase_order"):
        po_t = Table("purchase_order")
        po_sql = (
            Q.from_(po_t)
            .select(fn.Count("*").as_("cnt"))
            .where(po_t.company_id == P())
            .where(po_t.status.isin(_OPEN_ORDER_STATUSES))
            .get_sql()
        )
        open_po = conn.execute(po_sql, (company_id,)).fetchone()["cnt"]
    else:
        ops_missing.append("purchase_order")
    if open_so is None and open_po is None:
        operations = _unavailable("sales_order+purchase_order")
    else:
        operations = {"status": "available",
                      "open_sales_orders": open_so,
                      "open_purchase_orders": open_po}
        if ops_missing:
            operations["unavailable_tables"] = ops_missing

    ok({
        "company_id": company_id,
        "week_start": week_start,
        "week_end": week_end,
        "sections": sections,
        "sales": sales,
        "collections": collections,
        "spending": spending,
        "overdue_receivables": overdue,
        "operations": operations,
    })


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status_action(conn, args):
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    gl_t = Table("gl_entry").as_("g")
    acct_t = Table("account").as_("a")

    count_sql = (
        Q.from_(gl_t)
        .join(acct_t).on(acct_t.id == gl_t.account_id)
        .select(fn.Count("*").as_("cnt"))
        .where(acct_t.company_id == P())
        .where(gl_t.is_cancelled == 0)
        .get_sql()
    )
    gl_count = conn.execute(count_sql, (company_id,)).fetchone()["cnt"]

    dates_sql = (
        Q.from_(gl_t)
        .join(acct_t).on(acct_t.id == gl_t.account_id)
        .select(
            fn.Min(gl_t.posting_date).as_("earliest"),
            fn.Max(gl_t.posting_date).as_("latest"),
        )
        .where(acct_t.company_id == P())
        .where(gl_t.is_cancelled == 0)
        .get_sql()
    )
    dates = conn.execute(dates_sql, (company_id,)).fetchone()

    fy_t = Table("fiscal_year")
    fy_count_sql = (
        Q.from_(fy_t)
        .select(fn.Count("*").as_("cnt"))
        .where(fy_t.company_id == P())
        .get_sql()
    )
    fy_count = conn.execute(fy_count_sql, (company_id,)).fetchone()["cnt"]

    ok({
        "gl_entry_count": gl_count,
        "latest_posting_date": dates["latest"],
        "earliest_posting_date": dates["earliest"],
        "fiscal_years": fy_count,
    })


# ---------------------------------------------------------------------------
# Check Overdue Invoices
# ---------------------------------------------------------------------------


def check_overdue(conn, args):
    """Find overdue sales invoices and group them into aging buckets.

    SCOPE NOTE — F18, the two-truths ruling (ADR-0032 Decision 2). ERPClaw has
    exactly TWO sources of truth for what is owed, and no reader may invent a
    third:

      1. PER DOCUMENT: ``sales_invoice`` / ``purchase_invoice``.
         ``outstanding_amount`` — bound always-on by INV-25.
      2. PER PARTY: the payment-ledger net under
         ``erpclaw_lib.party_ledger``'s liveness + attribution rules — bound
         always-on by INV-27.

    They are equal by INV-25 per document and therefore by summation per party.
    This report DELIBERATELY keeps reading the document column: it is a
    per-document, due-date-filtered report, and the due date lives on the
    invoice, not on the ledger. That is a scope choice, not a third truth —
    ``ar-aging`` and ``get-outstanding`` read the party ledger for the same
    numbers at party granularity, and a Part A pin asserts the two agree for the
    same invoice set. What is forbidden is two readers of the SAME quantity
    disagreeing on scope or attribution.
    """
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    today = datetime.now().strftime("%Y-%m-%d")

    si_t = Table("sales_invoice").as_("si")
    cust_t = Table("customer").as_("c")

    sql = (
        Q.from_(si_t)
        .left_join(cust_t).on(cust_t.id == si_t.customer_id)
        .select(
            si_t.id, si_t.naming_series, si_t.grand_total,
            si_t.outstanding_amount, si_t.due_date,
            cust_t.name.as_("customer_name"),
        )
        .where(si_t.company_id == P())
        .where(si_t.status.isin(["submitted", "partially_paid", "overdue"]))
        .where(si_t.due_date < P())
        .orderby(si_t.due_date)
        .get_sql()
    )
    rows = conn.execute(sql, (company_id, today)).fetchall()

    # Initialize buckets
    buckets = {
        "0_30": {"count": 0, "total": Decimal("0")},
        "31_60": {"count": 0, "total": Decimal("0")},
        "61_90": {"count": 0, "total": Decimal("0")},
        "90_plus": {"count": 0, "total": Decimal("0")},
    }

    total_overdue = Decimal("0")
    invoices = []

    today_dt = datetime.strptime(today, "%Y-%m-%d")

    for row in rows:
        outstanding = _d(row["outstanding_amount"])
        if outstanding <= 0:
            continue
        due_date = row["due_date"]
        due_dt = datetime.strptime(due_date, "%Y-%m-%d")
        days_overdue = (today_dt - due_dt).days

        total_overdue += outstanding

        # Place into bucket
        if days_overdue <= 30:
            buckets["0_30"]["count"] += 1
            buckets["0_30"]["total"] += outstanding
        elif days_overdue <= 60:
            buckets["31_60"]["count"] += 1
            buckets["31_60"]["total"] += outstanding
        elif days_overdue <= 90:
            buckets["61_90"]["count"] += 1
            buckets["61_90"]["total"] += outstanding
        else:
            buckets["90_plus"]["count"] += 1
            buckets["90_plus"]["total"] += outstanding

        invoices.append({
            "id": row["id"],
            "name": row["naming_series"] or "",
            "customer_name": row["customer_name"] or "",
            "grand_total": _s(_d(row["grand_total"])),
            "outstanding": _s(outstanding),
            "due_date": due_date,
            "days_overdue": days_overdue,
        })

    # Sort by days_overdue descending
    invoices.sort(key=lambda x: x["days_overdue"], reverse=True)

    # Format bucket totals as strings
    formatted_buckets = {}
    for key, bucket in buckets.items():
        formatted_buckets[key] = {
            "count": bucket["count"],
            "total": _s(bucket["total"]),
        }

    ok({
        "overdue_count": len(invoices),
        "total_overdue": _s(total_overdue),
        "buckets": formatted_buckets,
        "invoices": invoices,
    })


# ---------------------------------------------------------------------------
# Intercompany Elimination — RETIRED (M63-C, 2026-08-12)
#
# These four actions drove a second, parallel elimination system: an operator
# declared account-pair rules in `elimination_rule`, and `run-elimination` posted
# the resulting "eliminations" straight into live `gl_entry` with raw SQL,
# bypassing `erpclaw_lib.gl_posting.insert_gl_entries` and its 12-step
# validation. Three things were wrong with that at once:
#
#   1. The pair spanned two companies — DR the source company's income account,
#      CR the target company's expense account — so neither operating entity's
#      own trial balance balanced afterwards (measured: the target company came
#      out 1,000.00 short on a single 1,000.00 elimination). ADR-0010 is explicit
#      that consolidation-level adjustments affect the GROUP statements only and
#      leave subsidiary books untouched.
#   2. Both tables are owned by the erpclaw-growth addon (`init_schema.py` says
#      so in its own comment); a foundation action wrote them, which the
#      ownership rule forbids, and on a foundation-only install neither table
#      exists so the action could not run at all.
#   3. The real system was already here and behaviorally tested:
#      erpclaw-accounting-adv keeps eliminations in the consolidation layer
#      (`advacct_elimination_entry`), where they belong.
#
# The action names stay ROUTABLE on purpose. An agent or an old script that asks
# for an intercompany elimination gets one JSON error naming the flow that does
# the job, instead of "Unknown action" or a `no such table` traceback. The two
# tables are dropped by erpclaw-growth migration 007, which archives any rows it
# finds first. Already-posted elimination GL is left exactly where it is —
# submitted ledger rows are immutable, and reversing an operator's books from a
# migration is not ours to do.
#
# ---------------------------------------------------------------------------

# Every step of this is required, and the two approval steps are the reason the
# sequence is spelled out rather than summarised: `add-ic-transaction` creates a
# DRAFT, and `generate-elimination-entries` only eliminates transactions whose
# ic_status is 'posted' (erpclaw-accounting-adv/consolidation.py). A caller who
# skips approve/post gets {"entries_created": 0, "status": "ok"} — a silent
# nothing, which is a worse answer than the error this steer replaces.
_ELIMINATION_STEER = (
    "Use the consolidation flow: add-consolidation-group -> add-group-entity "
    "(one per entity) -> add-ic-transaction -> approve-ic-transaction -> "
    "post-ic-transaction -> generate-elimination-entries -> "
    "consolidation-trial-balance-report / ic-elimination-report. "
    "approve- and post- are not optional: generate-elimination-entries only "
    "eliminates POSTED intercompany transactions, so a draft one is silently "
    "skipped."
)


def _retired_elimination(action):
    """Answer a retired elimination action with a steer to the real flow.

    One shared message for all four: four hand-written variants would drift, and
    the steer is the only thing a caller gets, so it is the part that has to stay
    correct. Exits 1 with a single JSON object via the standard `err` contract —
    never a traceback.
    """
    err(
        f"'{action}' has been retired. Intercompany eliminations are no longer "
        f"posted into the operating companies' books; they belong to the "
        f"consolidation layer (ADR-0010).",
        suggestion=_ELIMINATION_STEER,
    )


def add_elimination_rule(conn, args):
    """RETIRED — see the block above. Steers to the consolidation flow."""
    _retired_elimination("add-elimination-rule")


def list_elimination_rules(conn, args):
    """RETIRED — see the block above. Steers to the consolidation flow."""
    _retired_elimination("list-elimination-rules")


def run_elimination(conn, args):
    """RETIRED — see the block above. Steers to the consolidation flow."""
    _retired_elimination("run-elimination")


def list_elimination_entries(conn, args):
    """RETIRED — see the block above. Steers to the consolidation flow."""
    _retired_elimination("list-elimination-entries")


# ---------------------------------------------------------------------------
# Continuous Close Readiness
# ---------------------------------------------------------------------------

_CLOSE_READINESS_LIMITATION = (
    "This report is a read-only readiness preview. It does not perform, "
    "approve, or complete a period close, and it does not substitute for "
    "human review."
)


def _close_readiness_check(name, rule, facts, explanation, details):
    verdict = evaluate_rule(json.dumps(rule), json.dumps(facts))
    return {
        "name": name,
        "rule": rule,
        "facts": facts,
        "matched": verdict["matched"],
        "result": "pass" if verdict["matched"] else "block",
        "explanation": explanation,
        "details": details,
    }


def continuous_close_readiness(conn, args):
    if not getattr(args, "company_id", None) and not getattr(
            args, "company_name", None):
        err("--company-id is required")
    raw_asof = getattr(args, "as_of_date", None) or getattr(
        args, "to_date", None)
    if not raw_asof:
        err("--as-of-date is required")
    try:
        asof_dt = datetime.strptime(raw_asof, "%Y-%m-%d")
        if asof_dt.strftime("%Y-%m-%d") != raw_asof:
            raise ValueError()
    except (TypeError, ValueError):
        err("Invalid --as-of-date '%s': expected YYYY-MM-DD" % raw_asof)
    as_of = asof_dt.strftime("%Y-%m-%d")

    company_id = resolve_company_id(conn,
                                    getattr(args, "company_id", None),
                                    getattr(args, "company_name", None))
    comp_t = Table("company")
    found = conn.execute(
        Q.from_(comp_t).select(comp_t.id)
        .where(comp_t.id == P()).get_sql(),
        (company_id,)).fetchone()
    if not found:
        err("Company not found: %s" % company_id)

    je_t = Table("journal_entry")
    draft_sql = (
        Q.from_(je_t)
        .select(je_t.id, je_t.posting_date)
        .where(je_t.company_id == P())
        .where(je_t.status == "draft")
        .where(je_t.posting_date <= P())
        .orderby(je_t.id)
        .get_sql()
    )
    draft_rows = conn.execute(draft_sql, (company_id, as_of)).fetchall()
    draft_ids = sorted(row["id"] for row in draft_rows)

    posted_sql = (
        Q.from_(je_t)
        .select(je_t.id, je_t.total_debit, je_t.total_credit)
        .where(je_t.company_id == P())
        .where(je_t.status == "submitted")
        .where(je_t.posting_date <= P())
        .orderby(je_t.id)
        .get_sql()
    )
    posted_rows = conn.execute(posted_sql, (company_id, as_of)).fetchall()
    imbalanced = {}
    for row in posted_rows:
        debit = _d(row["total_debit"])
        credit = _d(row["total_credit"])
        if debit != credit:
            imbalanced[("journal_entry", row["id"])] = {
                "voucher_type": "journal_entry",
                "voucher_id": row["id"],
                "total_debit": _s(debit),
                "total_credit": _s(credit),
            }

    gl_t = Table("gl_entry").as_("g")
    acct_t = Table("account").as_("a")
    voucher_sql = (
        Q.from_(gl_t)
        .join(acct_t).on(acct_t.id == gl_t.account_id)
        .select(
            gl_t.voucher_type,
            gl_t.voucher_id,
            fn.Coalesce(DecimalSum(gl_t.debit), "0").as_("total_debit"),
            fn.Coalesce(DecimalSum(gl_t.credit), "0").as_("total_credit"),
        )
        .where(acct_t.company_id == P())
        .where(gl_t.is_cancelled == 0)
        .where(gl_t.posting_date <= P())
        .groupby(gl_t.voucher_type, gl_t.voucher_id)
        .get_sql()
    )
    voucher_rows = conn.execute(voucher_sql, (company_id, as_of)).fetchall()
    for row in voucher_rows:
        debit = _d(row["total_debit"])
        credit = _d(row["total_credit"])
        if debit != credit:
            key = (row["voucher_type"], row["voucher_id"])
            if key not in imbalanced:
                imbalanced[key] = {
                    "voucher_type": row["voucher_type"],
                    "voucher_id": row["voucher_id"],
                    "total_debit": _s(debit),
                    "total_credit": _s(credit),
                }
    imbalanced_vouchers = [imbalanced[k] for k in sorted(imbalanced)]

    pe_t = Table("payment_entry")
    pay_sql = (
        Q.from_(pe_t)
        .select(pe_t.id, pe_t.payment_type, pe_t.posting_date,
                pe_t.paid_amount, pe_t.unallocated_amount)
        .where(pe_t.company_id == P())
        .where(pe_t.status == "submitted")
        .where(pe_t.posting_date <= P())
        .orderby(pe_t.id)
        .get_sql()
    )
    pay_rows = conn.execute(pay_sql, (company_id, as_of)).fetchall()
    unallocated = []
    unallocated_total = Decimal("0")
    for row in pay_rows:
        residual = _d(row["unallocated_amount"])
        if residual > 0:
            unallocated.append({
                "payment_entry_id": row["id"],
                "payment_type": row["payment_type"],
                "posting_date": row["posting_date"],
                "paid_amount": _s(_d(row["paid_amount"])),
                "unallocated_amount": _s(residual),
            })
            unallocated_total += residual
    unallocated.sort(key=lambda e: e["payment_entry_id"])

    fy_t = Table("fiscal_year")
    fy_sql = (
        Q.from_(fy_t)
        .select(fy_t.id, fy_t.name, fy_t.start_date, fy_t.end_date)
        .where(fy_t.company_id == P())
        .where(fy_t.is_closed == 0)
        .where(fy_t.start_date <= P())
        .where(fy_t.end_date >= P())
        .orderby(fy_t.id)
        .get_sql()
    )
    fy_rows = conn.execute(fy_sql, (company_id, as_of, as_of)).fetchall()
    open_fy = fy_rows[0] if fy_rows else None

    if draft_ids:
        draft_explanation = (
            "%d draft journal %s waiting through %s. Drafts are not in "
            "the ledger yet, so the books may still move; submit or cancel "
            "them before closing."
            % (len(draft_ids),
               "entries are" if len(draft_ids) != 1 else "entry is",
               as_of))
    else:
        draft_explanation = (
            "No draft journal entries remain through %s; every journal is "
            "submitted or cancelled." % as_of)
    draft_check = _close_readiness_check(
        "draft_journal_entries",
        {"match": "all", "conditions": [
            {"field": "draft_count", "operator": "=", "value": "0"}]},
        {"draft_count": str(len(draft_ids))},
        draft_explanation,
        [{"journal_entry_id": i} for i in draft_ids])

    if imbalanced_vouchers:
        imbalance_explanation = (
            "%d posted %s do not balance: total debits differ from total "
            "credits. A close needs every voucher balanced; correct and "
            "repost them before closing."
            % (len(imbalanced_vouchers),
               "vouchers" if len(imbalanced_vouchers) != 1 else "voucher"))
    else:
        imbalance_explanation = (
            "Every posted voucher through %s balances: total debits equal "
            "total credits." % as_of)
    imbalance_check = _close_readiness_check(
        "balanced_posted_vouchers",
        {"match": "all", "conditions": [
            {"field": "imbalanced_voucher_count", "operator": "=",
             "value": "0"}]},
        {"imbalanced_voucher_count": str(len(imbalanced_vouchers))},
        imbalance_explanation,
        imbalanced_vouchers)

    if unallocated_total > 0:
        unallocated_explanation = (
            "Submitted payments still hold %s unapplied through %s. Apply "
            "or refund the open amounts so party balances are final before "
            "closing." % (_s(unallocated_total), as_of))
    else:
        unallocated_explanation = (
            "No submitted payment holds unapplied amounts through %s."
            % as_of)
    unallocated_check = _close_readiness_check(
        "allocated_submitted_payments",
        {"match": "all", "conditions": [
            {"field": "unallocated_total", "operator": "<=",
             "value": "0.00"}]},
        {"unallocated_total": _s(unallocated_total)},
        unallocated_explanation,
        unallocated)

    if open_fy is None:
        fy_explanation = (
            "No open fiscal year contains %s, so there is no open period "
            "to close into." % as_of)
    else:
        fy_explanation = (
            "Fiscal year '%s' is open and contains %s."
            % (open_fy["name"], as_of))
    fy_check = _close_readiness_check(
        "open_fiscal_year_contains_date",
        {"match": "all", "conditions": [
            {"field": "open_fiscal_year_count", "operator": ">=",
             "value": "1"}]},
        {"open_fiscal_year_count": str(len(fy_rows))},
        fy_explanation,
        [{"fiscal_year_id": r["id"], "name": r["name"],
          "start_date": r["start_date"], "end_date": r["end_date"]}
         for r in fy_rows])

    checks = [draft_check, imbalance_check, unallocated_check, fy_check]
    ready = all(c["matched"] for c in checks)

    ok({
        "company_id": company_id,
        "as_of_date": as_of,
        "ready": ready,
        "preview_only": True,
        "limitation": _CLOSE_READINESS_LIMITATION,
        "draft_journal_count": len(draft_ids),
        "draft_journal_ids": draft_ids,
        "imbalanced_voucher_count": len(imbalanced_vouchers),
        "imbalanced_vouchers": imbalanced_vouchers,
        "unallocated_payment_count": len(unallocated),
        "unallocated_total": _s(unallocated_total),
        "unallocated_payments": unallocated,
        "open_fiscal_year": open_fy is not None,
        "fiscal_year_id": open_fy["id"] if open_fy is not None else None,
        "checks": checks,
    })


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Multi-dimensional reports (M6)
# ---------------------------------------------------------------------------

def multi_dim_trial_balance(conn, args):
    """Trial balance grouped by one or more accounting dimensions.

    --group-by "project,department" groups gl_entry rows by each dimension's value
    (read from dimensions_json via json_get) and sums debit/credit per group.
    """
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    if not args.to_date:
        err("--to-date is required")
    group_by = [k.strip() for k in (getattr(args, "group_by", None) or "").split(",")
                if k.strip()]
    if not group_by:
        err('--group-by "project,department" is required')

    select_cols, group_exprs = [], []
    for k in group_by:
        frag = str(json_get("g.dimensions_json", k))  # dialect-aware, key-escaped
        select_cols.append(f'{frag} AS "{k}"')
        group_exprs.append(frag)

    where = "a.company_id = ? AND g.is_cancelled = 0 AND g.posting_date <= ?"
    params = [company_id, args.to_date]
    if args.from_date:
        where += " AND g.posting_date >= ?"
        params.append(args.from_date)
    dim_clause, dim_params = _dimension_filter(args, alias="g")
    where += dim_clause
    params += dim_params

    # select_cols/group_exprs are json_get fragments (escaped keys); `where` carries
    # bound ? placeholders. Concatenated (not f-string) so the intentional identifier
    # interpolation is explicit and every value stays bound.
    group_sql = ", ".join(group_exprs)
    sql = (
        "SELECT " + ", ".join(select_cols) + ", "
        "COALESCE(decimal_sum(g.debit), '0') AS total_debit, "
        "COALESCE(decimal_sum(g.credit), '0') AS total_credit "
        "FROM gl_entry g JOIN account a ON a.id = g.account_id "
        "WHERE " + where + " "
        "GROUP BY " + group_sql + " "
        "ORDER BY " + group_sql
    )
    rows = conn.execute(sql, params).fetchall()

    groups = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for r in rows:
        d = _d(r["total_debit"])
        c = _d(r["total_credit"])
        total_debit += d
        total_credit += c
        group = {k: r[k] for k in group_by}
        group["debit"] = _s(d)
        group["credit"] = _s(c)
        group["balance"] = _s(d - c)
        groups.append(group)

    ok({
        "group_by": group_by,
        "as_of_date": args.to_date,
        "groups": groups,
        "total_debit": _s(total_debit),
        "total_credit": _s(total_credit),
    })


def dimension_balance_report(conn, args):
    """Balance per value of a single accounting dimension.

    --dimension K returns the net balance (debit - credit) for each distinct value
    of dimension K; optional --values "a,b,c" restricts to those values.
    """
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))
    key = (getattr(args, "dimension", None) or "").strip()
    if not key:
        err("--dimension K is required")
    if not args.to_date:
        err("--to-date is required")

    frag = str(json_get("g.dimensions_json", key))  # dialect-aware, key-escaped
    where = "a.company_id = ? AND g.is_cancelled = 0 AND g.posting_date <= ?"
    params = [company_id, args.to_date]
    if args.from_date:
        where += " AND g.posting_date >= ?"
        params.append(args.from_date)

    values = [v.strip() for v in (getattr(args, "values", None) or "").split(",")
              if v.strip()]
    if values:
        placeholders = ",".join("?" for _ in values)
        where += " AND " + frag + " IN (" + placeholders + ")"
        params += values

    # frag is a json_get fragment (escaped key); `where` carries bound ? values.
    # Concatenated (not f-string) so identifier interpolation is explicit/bound-safe.
    sql = (
        "SELECT " + frag + " AS dim_value, "
        "COALESCE(decimal_sum(g.debit), '0') AS total_debit, "
        "COALESCE(decimal_sum(g.credit), '0') AS total_credit "
        "FROM gl_entry g JOIN account a ON a.id = g.account_id "
        "WHERE " + where + " AND " + frag + " IS NOT NULL "
        "GROUP BY " + frag + " ORDER BY dim_value"
    )
    rows = conn.execute(sql, params).fetchall()

    out = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for r in rows:
        d = _d(r["total_debit"])
        c = _d(r["total_credit"])
        total_debit += d
        total_credit += c
        out.append({
            "value": r["dim_value"],
            "debit": _s(d),
            "credit": _s(c),
            "balance": _s(d - c),
        })

    ok({
        "dimension": key,
        "as_of_date": args.to_date,
        "values": out,
        "total_debit": _s(total_debit),
        "total_credit": _s(total_credit),
    })


_CSV_REPORTS = frozenset({
    "trial-balance", "profit-and-loss", "balance-sheet", "general-ledger",
})


def _csv_cell(value):
    """Keep exact amount strings and quote spreadsheet formulas as text."""
    if value is None:
        return ""
    text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return "'" + text
        if not amount.is_finite():
            return "'" + text
    return text


def export_financial_csv(conn, args):
    """Export existing statement results without changing filters or books."""
    if args.action not in _CSV_REPORTS:
        err("CSV export supports trial-balance, profit-and-loss, balance-sheet and general-ledger")
    captured = io.StringIO()
    exit_code = 0
    with redirect_stdout(captured):
        try:
            ACTIONS[args.action](conn, args)
        except SystemExit as result:
            exit_code = result.code
    if exit_code != 0:
        print(captured.getvalue(), end="")
        sys.exit(exit_code)
    data = json.loads(captured.getvalue())
    rows = []
    for section, value in data.items():
        if section == "status":
            continue
        if isinstance(value, list):
            for item in value:
                rows.append({"section": section, **item})
        else:
            rows.append({"section": "summary", "metric": section, "value": value})
    columns = ["section"]
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows({key: _csv_cell(value) for key, value in row.items()} for row in rows)
    ok({"report": args.action, "format": "csv", "row_count": len(rows),
        "columns": columns, "csv": stream.getvalue()})
def nonprofit_statement_set(conn, args):
    """Read a two-class statement set from explicitly classified company books."""
    from datetime import date

    company_id = resolve_company_id(conn, getattr(args, "company_id", None),
                                    getattr(args, "company_name", None))
    start, end = getattr(args, "from_date", None), getattr(args, "to_date", None)
    try:
        if not start or not end or date.fromisoformat(start) > date.fromisoformat(end) or (
                date.fromisoformat(start).isoformat() != start or
                date.fromisoformat(end).isoformat() != end):
            raise ValueError()
    except (TypeError, ValueError):
        err("Valid --from-date and --to-date in ascending order are required")
    key = (getattr(args, "net_asset_dimension", None) or "").strip()
    if not key:
        err("--net-asset-dimension is required; untagged entries are not classified")
    registry = Table("dimension_registry")
    registered = conn.execute(Q.from_(registry).select(
        registry.is_active, registry.data_type, registry.allowed_values_json).where(
        registry.key == P()).get_sql(), (key,)).fetchone()
    if not registered or not registered["is_active"]:
        err("The net-asset dimension must be registered and active")
    classes = ("without_donor_restrictions", "with_donor_restrictions")
    try:
        declared_classes = json.loads(registered["allowed_values_json"] or "null")
    except (TypeError, ValueError):
        declared_classes = None
    if registered["data_type"] != "enum" or not isinstance(declared_classes, list) or (
            any(not isinstance(value, str) for value in declared_classes)) or (
            set(declared_classes) != set(classes)):
        err("The registered dimension must declare exactly the two donor-restriction classes")
    mapping = _parse_json_arg(getattr(args, "cash_flow_account_map", None),
                              "cash-flow-account-map")
    categories = ("operating", "investing", "financing")
    if not isinstance(mapping, dict) or any(
            not isinstance(account_id, str) or category not in categories
            for account_id, category in mapping.items()):
        err("--cash-flow-account-map must map non-cash account IDs to operating, investing or financing")
    releases = set(v.strip() for v in
                   (getattr(args, "release_voucher_types", None) or "").split(",") if v.strip())
    if _CLOSING_VOUCHER_TYPE in releases:
        err("Period-closing vouchers cannot be release vouchers")
    position = {name: {"assets": Decimal("0"), "liabilities": Decimal("0"),
                       "net_assets": Decimal("0")} for name in classes}
    activity = {name: {"opening_net_assets": Decimal("0"), "revenue": Decimal("0"),
                       "releases": Decimal("0"), "expenses": Decimal("0"),
                       "change_in_net_assets": Decimal("0"),
                       "closing_net_assets": Decimal("0")} for name in classes}
    flow = {name: Decimal("0") for name in categories}
    opening_cash = closing_cash = Decimal("0")
    release_groups = {}
    flow_groups = {}
    accounts = Table("account")
    entries = Table("gl_entry")
    query = Q.from_(entries).join(accounts).on(entries.account_id == accounts.id).select(
        entries.id, entries.posting_date, entries.debit_base, entries.credit_base,
        entries.dimensions_json, entries.voucher_type, entries.voucher_id,
        accounts.id.as_("account_id"), accounts.root_type, accounts.account_type
    ).where(accounts.company_id == P()).where(entries.is_cancelled == 0).where(
        entries.posting_date <= P())
    rows = conn.execute(query.get_sql(), (company_id, end)).fetchall()
    for row in rows:
        try:
            tags = json.loads(row["dimensions_json"] or "{}")
        except (TypeError, ValueError):
            err("A ledger entry has invalid accounting dimensions")
        if not isinstance(tags, dict) or tags.get(key) not in classes:
            err("Every included ledger entry needs a recognised net-asset class tag")
        class_name = tags[key]
        debit, credit = _d(row["debit_base"]), _d(row["credit_base"])
        if not debit.is_finite() or not credit.is_finite():
            err("Ledger amounts must be finite exact decimals")
        movement = debit - credit
        root = row["root_type"]
        period = row["posting_date"] >= start
        is_release = row["voucher_type"] in releases
        is_closing = row["voucher_type"] == _CLOSING_VOUCHER_TYPE
        if is_release and root != "equity":
            err("Named release vouchers must contain only net-asset equity transfers")
        if root == "asset":
            position[class_name]["assets"] += movement
        elif root == "liability":
            position[class_name]["liabilities"] -= movement
        elif root in ("equity", "income", "expense"):
            position[class_name]["net_assets"] -= movement
            activity[class_name]["closing_net_assets"] -= movement
            if not period:
                activity[class_name]["opening_net_assets"] -= movement
        else:
            err("Unsupported account root in the nonprofit statement set")
        cash = root == "asset" and row["account_type"] in ("bank", "cash")
        if cash:
            closing_cash += movement
            if not period:
                opening_cash += movement
        if not period or is_closing:
            continue
        voucher = (row["voucher_type"], row["voucher_id"])
        if not is_release:
            group = flow_groups.setdefault(voucher, {
                "cash": Decimal("0"), **{name: Decimal("0") for name in categories}})
            if cash:
                group["cash"] += movement
        if is_release:
            activity[class_name]["releases"] -= movement
            release_groups.setdefault(voucher, {name: Decimal("0") for name in classes})
            release_groups[voucher][class_name] -= movement
        elif root == "income":
            activity[class_name]["revenue"] -= movement
        elif root == "expense":
            if class_name != "without_donor_restrictions":
                err("Expenses must reduce net assets without donor restrictions")
            activity[class_name]["expenses"] += movement
        elif root == "equity" and movement:
            err("Period equity movements require an explicit release voucher type")
        if not cash and not is_release and movement:
            category = mapping.get(row["account_id"])
            if category is None:
                err("Every non-cash account with period movement needs a cash-flow category")
            group[category] -= movement
    for group in flow_groups.values():
        classified = sum((group[name] for name in categories), Decimal("0"))
        if classified != group["cash"]:
            err("Each voucher's classified cash flows must reconcile to its cash movement")
        if not group["cash"] and any(group[name] for name in categories):
            err("Non-cash vouchers cannot create classified cash flows")
        for name in categories:
            flow[name] += group[name]
    for balances in release_groups.values():
        if sum(balances.values(), Decimal("0")) != 0 or (
                balances["without_donor_restrictions"] < 0):
            err("Each release voucher must balance between the two classes in the release direction")
    for class_name in classes:
        values = activity[class_name]
        values["change_in_net_assets"] = values["revenue"] + values["releases"] - values["expenses"]
        if values["opening_net_assets"] + values["change_in_net_assets"] != values["closing_net_assets"]:
            err("Net-asset opening, activity and closing balances do not reconcile")
    totals = {name: sum((p[name] for p in position.values()), Decimal("0"))
              for name in ("assets", "liabilities", "net_assets")}
    if totals["assets"] - totals["liabilities"] != totals["net_assets"]:
        err("The company financial position does not balance")
    cash_change = closing_cash - opening_cash
    if sum(flow.values(), Decimal("0")) != cash_change:
        err("Classified cash flows do not reconcile to the cash balance movement")
    net_asset_change = sum((a["change_in_net_assets"] for a in activity.values()), Decimal("0"))
    ok({
        "company_id": company_id, "from_date": start, "to_date": end,
        "net_asset_dimension": key, "basis": "company base currency, explicitly tagged posted ledger",
        "financial_position": {"classes": {name: {k: _s(v) for k, v in p.items()}
                                              for name, p in position.items()},
                               "totals": {k: _s(v) for k, v in totals.items()}},
        "activities": {name: {k: _s(v) for k, v in a.items()} for name, a in activity.items()},
        "cash_flow": {**{k: _s(v) for k, v in flow.items()},
                      "opening_cash": _s(opening_cash), "closing_cash": _s(closing_cash),
                      "net_change": _s(cash_change), "change_in_net_assets": _s(net_asset_change),
                      "operating_reconciliation_adjustment": _s(flow["operating"] - net_asset_change)},
    })


def sefa_readiness_report(conn, args):
    """Prepare an award worksheet from explicitly mapped approved expenses."""
    company_id = getattr(args, "company_id", None)
    fiscal_year_id = getattr(args, "fiscal_year_id", None)
    if not company_id or not fiscal_year_id:
        err("--company-id and --fiscal-year-id are required")
    company = Table("company")
    owner = conn.execute(Q.from_(company).select(company.default_currency)
                         .where(company.id == P()).get_sql(),
                         (company_id,)).fetchone()
    if not owner:
        err("Company not found")
    fy = Table("fiscal_year")
    period = conn.execute(Q.from_(fy).select(fy.start_date, fy.end_date)
                          .where(fy.id == P()).where(fy.company_id == P())
                          .get_sql(), (fiscal_year_id, company_id)).fetchone()
    if not period:
        err("Fiscal year not found for this company")
    start, end = period["start_date"], period["end_date"]
    try:
        if (datetime.strptime(start, "%Y-%m-%d").strftime("%Y-%m-%d") != start
                or datetime.strptime(end, "%Y-%m-%d").strftime("%Y-%m-%d") != end
                or start > end):
            raise ValueError
    except (ValueError, TypeError):
        err("Fiscal year must have valid ordered ISO dates")
    try:
        awards = json.loads(getattr(args, "federal_awards", None))
    except (ValueError, TypeError):
        err("--federal-awards must be a JSON array of award objects")
    if not isinstance(awards, list) or len(awards) > 500:
        err("--federal-awards must be an array with at most 500 awards")
    mapped = {}
    warnings = []
    fields = ("agency_name", "assistance_listing_number", "award_identifier",
              "pass_through_entity")
    for award in awards:
        if not isinstance(award, dict) or not isinstance(award.get("grant_id"), str):
            err("Every award must name a grant_id")
        grant_id = award["grant_id"].strip()
        if not grant_id or grant_id in mapped or len(grant_id) > 200:
            err("Award grant IDs must be nonempty and unique")
        metadata = {}
        for field in fields:
            value = award.get(field, "")
            if not isinstance(value, str) or len(value) > 200:
                err("Award metadata must be text of at most 200 characters")
            metadata[field] = value.strip()
        missing = [field for field in fields[:3] if not metadata[field]]
        if missing:
            warnings.append({"code": "missing_award_metadata", "grant_id": grant_id,
                             "fields": missing})
        mapped[grant_id] = {"grant_id": grant_id, **metadata,
                            "expense_ids": [], "expenditures": Decimal("0")}
    source_available = all(table_exists(conn, name) for name in
                           ("nonprofitclaw_grant", "nonprofitclaw_grant_expense"))
    omitted_ids = []
    omitted_total = Decimal("0")
    if not source_available:
        if mapped:
            err("NonprofitClaw grant and expense tables are required for awards")
        warnings.append({"code": "grant_expense_source_unavailable"})
    else:
        grant = Table("nonprofitclaw_grant")
        for grant_id, award in mapped.items():
            row = conn.execute(Q.from_(grant).select(grant.name)
                               .where(grant.id == P()).where(grant.company_id == P())
                               .get_sql(), (grant_id, company_id)).fetchone()
            if not row:
                err("Every mapped grant must belong to this company")
            award["grant_name"] = row["name"]
        expense = Table("nonprofitclaw_grant_expense")
        rows = conn.execute(Q.from_(expense).select(
            expense.id, expense.grant_id, expense.amount)
            .where(expense.company_id == P()).where(expense.status == P())
            .where(expense.expense_date >= P()).where(expense.expense_date <= P())
            .orderby(expense.id).get_sql(),
            (company_id, "approved", start, end)).fetchall()
        for row in rows:
            try:
                amount = Decimal(str(row["amount"]))
                if not amount.is_finite() or amount < 0:
                    raise InvalidOperation
                # Refuse values that cannot be represented as currency amounts.
                round_currency(amount)
            except (InvalidOperation, ValueError, TypeError):
                err("Approved grant expense contains an invalid amount")
            award = mapped.get(row["grant_id"])
            if award is None:
                omitted_ids.append(row["id"])
                omitted_total += amount
            else:
                award["expense_ids"].append(row["id"])
                award["expenditures"] += amount
    if omitted_ids:
        warnings.append({"code": "unclassified_approved_expenses",
                         "expense_ids": omitted_ids,
                         "amount": _s(omitted_total)})
    total = sum((award["expenditures"] for award in mapped.values()), Decimal("0"))
    try:
        for award in mapped.values():
            award["expense_count"] = len(award["expense_ids"])
            award["expenditures"] = _s(award["expenditures"])
        total_text = _s(total) if source_available else None
    except InvalidOperation:
        err("Grant expense totals exceed the supported currency precision")
    ok({"company_id": company_id, "fiscal_year_id": fiscal_year_id,
        "from_date": start, "to_date": end, "currency": owner["default_currency"],
        "basis": "approved grant expenses by expense date; explicit caller award mapping",
        "source_available": source_available, "awards": list(mapped.values()),
        "total_expenditures": total_text, "warnings": warnings,
        "ready_for_review": source_available and bool(mapped) and not warnings,
        "limitations": "Preparation worksheet only. Federal completeness, accounting basis, "
                       "subrecipient expenditure data and audit eligibility require review."})


def governmental_statement_set(conn, args):
    """Reconcile explicitly classified governmental funds and posted full-accrual books."""
    from datetime import date

    company = resolve_company_id(conn, getattr(args, "company_id", None),
                                 getattr(args, "company_name", None))
    start, end = getattr(args, "from_date", None), getattr(args, "to_date", None)
    try:
        if not start or not end or date.fromisoformat(start) > date.fromisoformat(end) or (
                date.fromisoformat(start).isoformat() != start or
                date.fromisoformat(end).isoformat() != end):
            raise ValueError()
    except (TypeError, ValueError):
        err("Valid --from-date and --to-date in ascending order are required")
    fund_key = (getattr(args, "fund_dimension", None) or "").strip()
    class_key = (getattr(args, "net_position_dimension", None) or "").strip()
    if not fund_key or not class_key or fund_key == class_key:
        err("Distinct --fund-dimension and --net-position-dimension are required")
    classes = ("net_investment_in_capital_assets", "restricted_expendable",
               "restricted_nonexpendable", "unrestricted")
    registry = Table("dimension_registry")
    declared = {}
    for key in (fund_key, class_key):
        registered = conn.execute(Q.from_(registry).select(
            registry.is_active, registry.data_type, registry.allowed_values_json).where(
            registry.key == P()).get_sql(), (key,)).fetchone()
        if not registered or not registered["is_active"] or registered["data_type"] != "enum":
            err("Both statement dimensions must be registered active enums")
        try:
            values = json.loads(registered["allowed_values_json"] or "null")
        except (TypeError, ValueError):
            values = None
        if not isinstance(values, list) or not values or any(
                not isinstance(v, str) or not v.strip() for v in values) or (
                len(set(values)) != len(values)):
            err("Statement dimensions must declare distinct non-empty string values")
        declared[key] = values
    if set(declared[class_key]) != set(classes):
        err("The net-position dimension must declare exactly the four supported classes")
    bases = _parse_json_arg(getattr(args, "fund_basis_map", None), "fund-basis-map")
    if not isinstance(bases, dict) or set(bases) != set(declared[fund_key]) or any(
            basis != "modified_accrual" for basis in bases.values()):
        err("--fund-basis-map must explicitly assign modified_accrual to every declared fund")
    roles = {"current_assets": "asset", "capital_assets": "asset",
             "deferred_outflows": "asset", "current_liabilities": "liability",
             "long_term_liabilities": "liability", "deferred_inflows": "liability",
             "net_position": "equity", "revenues": "income", "expenses": "expense"}
    mapping = _parse_json_arg(getattr(args, "government_account_map", None),
                              "government-account-map")
    if not isinstance(mapping, dict) or any(
            not isinstance(aid, str) or not isinstance(role, str) or role not in roles
            for aid, role in mapping.items()):
        err("--government-account-map must map account IDs to supported statement roles")
    accounts = Table("account")
    owned = {row["id"]: row["root_type"] for row in conn.execute(
        Q.from_(accounts).select(accounts.id, accounts.root_type).where(
            accounts.company_id == P()).get_sql(), (company,)).fetchall()}
    if any(aid not in owned or owned[aid] != roles[role] for aid, role in mapping.items()):
        err("Mapped accounts must belong to the company and have compatible account roots")
    conversions = _parse_json_arg(getattr(args, "conversion_voucher_types", None),
                                  "conversion-voucher-types")
    if not isinstance(conversions, list) or any(
            not isinstance(v, str) or not v.strip() or v == _CLOSING_VOUCHER_TYPE
            for v in conversions) or len(set(conversions)) != len(conversions):
        err("--conversion-voucher-types must be an explicit JSON list of non-closing voucher types")
    zero = Decimal("0")
    closing = {role: zero for role in roles}
    opening = closing.copy()
    class_activity = {name: {"opening_net_position": zero, "revenues": zero,
                            "expenses": zero, "other_changes": zero,
                            "closing_net_position": zero} for name in classes}
    funds = {name: {"basis": basis, "opening_fund_balance": zero,
                    "closing_fund_balance": zero, "revenues": zero,
                    "expenditures": zero, "other_financing_sources_and_uses": zero}
             for name, basis in bases.items()}
    conversion_current_open = conversion_current_close = zero
    voucher_balances = {}
    entries = Table("gl_entry")
    query = Q.from_(entries).join(accounts).on(entries.account_id == accounts.id).select(
        entries.account_id, entries.posting_date, entries.debit_base, entries.credit_base,
        entries.dimensions_json, entries.voucher_type, entries.voucher_id
    ).where(accounts.company_id == P()).where(entries.is_cancelled == 0).where(
        entries.posting_date <= P())
    for row in conn.execute(query.get_sql(), (company, end)).fetchall():
        try:
            tags = json.loads(row["dimensions_json"] or "{}")
        except (TypeError, ValueError):
            err("A ledger entry has invalid accounting dimensions")
        if not isinstance(tags, dict) or not isinstance(tags.get(fund_key), str) or (
                tags.get(fund_key) not in funds) or (
                tags.get(class_key) not in classes):
            err("Every included ledger leg needs recognised fund and net-position class tags")
        role = mapping.get(row["account_id"])
        if role is None:
            err("Every included account needs an explicit government statement role")
        try:
            debit, credit = _d(row["debit_base"]), _d(row["credit_base"])
        except (TypeError, ValueError):
            err("Ledger amounts must be finite non-negative exact decimals")
        if not debit.is_finite() or not credit.is_finite() or debit < 0 or credit < 0:
            err("Ledger amounts must be finite non-negative exact decimals")
        movement = debit - credit
        signed = movement if roles[role] in ("asset", "expense") else -movement
        period = row["posting_date"] >= start
        conversion = row["voucher_type"] in conversions
        is_closing = row["voucher_type"] == _CLOSING_VOUCHER_TYPE
        if not row["voucher_type"] or not row["voucher_id"]:
            err("Every included ledger leg needs a voucher type and ID")
        group = (tags[fund_key], row["voucher_type"], row["voucher_id"])
        voucher_balances[group] = voucher_balances.get(group, zero) + movement
        closing[role] += signed
        if not period:
            opening[role] += signed
        activity = class_activity[tags[class_key]]
        if role in ("net_position", "revenues", "expenses"):
            net = -movement
            activity["closing_net_position"] += net
            if not period:
                activity["opening_net_position"] += net
            elif not is_closing:
                field = {"net_position": "other_changes", "revenues": "revenues",
                         "expenses": "expenses"}[role]
                activity[field] += signed
        current_resource = (movement if role in (
            "current_assets", "current_liabilities", "deferred_inflows") else zero)
        if conversion:
            conversion_current_close += current_resource
            if not period:
                conversion_current_open += current_resource
            continue
        fund = funds[tags[fund_key]]
        fund["closing_fund_balance"] += current_resource
        if not period:
            fund["opening_fund_balance"] += current_resource
        elif not is_closing:
            if role == "revenues":
                fund["revenues"] += signed
            elif role in ("expenses", "capital_assets", "deferred_outflows"):
                fund["expenditures"] += movement
            elif role in ("net_position", "long_term_liabilities"):
                fund["other_financing_sources_and_uses"] += signed
    if any(balance != 0 for balance in voucher_balances.values()):
        err("Each included voucher must balance within its declared fund")
    for fund in funds.values():
        fund["change_in_fund_balance"] = (fund["revenues"] - fund["expenditures"] +
                                           fund["other_financing_sources_and_uses"])
        if fund["opening_fund_balance"] + fund["change_in_fund_balance"] != fund["closing_fund_balance"]:
            err("Fund opening balances and classified period activity do not reconcile")
    for activity in class_activity.values():
        activity["change_in_net_position"] = (activity["revenues"] - activity["expenses"] +
                                               activity["other_changes"])
        if activity["opening_net_position"] + activity["change_in_net_position"] != activity["closing_net_position"]:
            err("Net-position classes and period activity do not reconcile")
    net_position = (closing["current_assets"] + closing["capital_assets"] +
                    closing["deferred_outflows"] - closing["current_liabilities"] -
                    closing["long_term_liabilities"] - closing["deferred_inflows"])
    if net_position != sum((a["closing_net_position"] for a in class_activity.values()), zero):
        err("Government-wide financial position does not balance")
    position_bridge = {
        "fund_balances": sum((f["closing_fund_balance"] for f in funds.values()), zero),
        "capital_assets": closing["capital_assets"],
        "deferred_outflows": closing["deferred_outflows"],
        "long_term_liabilities": -closing["long_term_liabilities"],
        "conversion_current_resources": conversion_current_close}
    activity_bridge = {
        "fund_balance_change": sum((f["change_in_fund_balance"] for f in funds.values()), zero),
        "capital_asset_change": closing["capital_assets"] - opening["capital_assets"],
        "deferred_outflow_change": closing["deferred_outflows"] - opening["deferred_outflows"],
        "long_term_liability_change": opening["long_term_liabilities"] - closing["long_term_liabilities"],
        "conversion_current_resource_change": conversion_current_close - conversion_current_open}
    net_change = sum((a["change_in_net_position"] for a in class_activity.values()), zero)
    if sum(position_bridge.values(), zero) != net_position or sum(activity_bridge.values(), zero) != net_change:
        err("Fund and government-wide reconciliations do not agree")
    ok({"company_id": company, "from_date": start, "to_date": end,
        "basis": "company base currency, explicitly classified posted books",
        "scope": "Governmental fund current-resource projection and full-accrual statement reconciliation; no automatic recognition, budget, lease, notes or account-root schema conversion",
        "fund_statements": {name: {k: _s(v) if isinstance(v, Decimal) else v
                                    for k, v in fund.items()} for name, fund in funds.items()},
        "statement_of_net_position": {
            **{k: _s(closing[k]) for k in roles if k not in ("net_position", "revenues", "expenses")},
            "net_position": _s(net_position),
            "classes": {name: _s(a["closing_net_position"]) for name, a in class_activity.items()}},
        "statement_of_activities": {name: {k: _s(v) for k, v in a.items()}
                                    for name, a in class_activity.items()},
        "reconciliation": {"position": {**{k: _s(v) for k, v in position_bridge.items()},
                                         "government_wide_net_position": _s(net_position)},
                           "activity": {**{k: _s(v) for k, v in activity_bridge.items()},
                                         "government_wide_change": _s(net_change)}}})


ACTIONS = {
    "trial-balance": trial_balance,
    "profit-and-loss": profit_and_loss,
    "balance-sheet": balance_sheet,
    "cash-flow": cash_flow,
    "general-ledger": general_ledger,
    "multi-dim-trial-balance": multi_dim_trial_balance,
    "dimension-balance-report": dimension_balance_report,
    "nonprofit-statement-set": nonprofit_statement_set,
    "sefa-readiness-report": sefa_readiness_report,
    "governmental-statement-set": governmental_statement_set,
    "ar-aging": ar_aging,
    "ap-aging": ap_aging,
    "budget-vs-actual": budget_vs_actual,
    "budget-variance": budget_vs_actual,  # alias
    "flux-variance-narrative": flux_variance_narrative,
    "party-ledger": party_ledger,
    "tax-summary": tax_summary,
    "payment-summary": payment_summary,
    "gl-summary": gl_summary,
    "comparative-pl": comparative_pl,
    "check-overdue": check_overdue,
    "weekly-digest": weekly_digest,
    "continuous-close-readiness": continuous_close_readiness,
    "add-elimination-rule": add_elimination_rule,
    "list-elimination-rules": list_elimination_rules,
    "run-elimination": run_elimination,
    "list-elimination-entries": list_elimination_entries,
    "status": status_action,
}


def main():
    parser = SafeArgumentParser(description="ERPClaw Reports Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--format", choices=("json", "csv"), default="json")

    # Common filters
    parser.add_argument("--company-id")
    parser.add_argument("--company", dest="company_name", default=None)  # NL: company by name
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--as-of-date")
    parser.add_argument("--start-date")
    parser.add_argument("--account-id")
    parser.add_argument("--cost-center-id")
    parser.add_argument("--project-id")

    # General ledger
    parser.add_argument("--party-type")
    parser.add_argument("--party-id")
    parser.add_argument("--voucher-type")

    # Accounting dimensions (M6)
    parser.add_argument("--dimension-key", dest="dimension_key", action="append")
    parser.add_argument("--dimension-value", dest="dimension_value", action="append")
    parser.add_argument("--group-by", dest="group_by")
    parser.add_argument("--dimension", dest="dimension")
    parser.add_argument("--values", dest="values")
    parser.add_argument("--net-asset-dimension")
    parser.add_argument("--cash-flow-account-map")
    parser.add_argument("--federal-awards")
    parser.add_argument("--release-voucher-types")
    parser.add_argument("--fund-dimension")
    parser.add_argument("--net-position-dimension")
    parser.add_argument("--fund-basis-map")
    parser.add_argument("--government-account-map")
    parser.add_argument("--conversion-voucher-types")

    # Aging
    parser.add_argument("--customer-id")
    parser.add_argument("--supplier-id")
    parser.add_argument("--aging-buckets", default="30,60,90,120")

    # Budget
    parser.add_argument("--fiscal-year-id")
    parser.add_argument("--materiality-amount", default="0.00")
    parser.add_argument("--materiality-percent", default="0.00")

    # P&L periodicity
    parser.add_argument("--periodicity", default="annual")

    # Comparative
    parser.add_argument("--periods")  # JSON string

    # Elimination
    parser.add_argument("--name")
    parser.add_argument("--target-company-id")
    parser.add_argument("--source-account-id")
    parser.add_argument("--target-account-id")
    parser.add_argument("--posting-date")

    # Pagination
    parser.add_argument("--limit", default="100")
    parser.add_argument("--offset", default="0")

    args, unknown = parser.parse_known_args()
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

    try:
        if args.format == "csv":
            export_financial_csv(conn, args)
        else:
            ACTIONS[args.action](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
