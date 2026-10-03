#!/usr/bin/env python3
"""ERPClaw Reports Skill — db_query.py

Read-only financial reporting. Owns NO tables — reads gl_entry,
payment_ledger_entry, account, budget, fiscal_year, etc.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal

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
    from erpclaw_lib.dependencies import check_required_tables
    from erpclaw_lib.query_helpers import resolve_company_id, resolve_scope_company
    from erpclaw_lib.voucher_types import canonical_voucher_type
    # Aliased: this module already has a `party_ledger` ACTION function (the
    # `party-ledger` report at :1049), and a bare import would be shadowed by it.
    from erpclaw_lib import party_ledger as party_ledger_rules
    from erpclaw_lib.query import Q, P, Table, Field, Case, fn, DecimalSum, DecimalAbs, json_get
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
        budget_amt = _d(b["budget_amount"])

        # Build actual query dynamically
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

        if b["account_id"]:
            actual_q = actual_q.where(gl_t.account_id == P())
            actual_params.append(b["account_id"])
        if b["cost_center_id"]:
            actual_q = actual_q.where(gl_t.cost_center_id == P())
            actual_params.append(b["cost_center_id"])

        actual = conn.execute(actual_q.get_sql(), actual_params).fetchone()
        actual_amt = _d(actual["total_debit"]) - _d(actual["total_credit"])

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

    gl_t = Table("gl_entry").as_("g")
    acct_t = Table("account")
    acct_scope_sub = (
        Q.from_(acct_t)
        .select(acct_t.id)
        .where(acct_t.company_id == P())
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
                          scope_company_id]
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
    entries_params = [args.party_type, args.party_id, scope_company_id]

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


ACTIONS = {
    "trial-balance": trial_balance,
    "profit-and-loss": profit_and_loss,
    "balance-sheet": balance_sheet,
    "cash-flow": cash_flow,
    "general-ledger": general_ledger,
    "multi-dim-trial-balance": multi_dim_trial_balance,
    "dimension-balance-report": dimension_balance_report,
    "ar-aging": ar_aging,
    "ap-aging": ap_aging,
    "budget-vs-actual": budget_vs_actual,
    "budget-variance": budget_vs_actual,  # alias
    "party-ledger": party_ledger,
    "tax-summary": tax_summary,
    "payment-summary": payment_summary,
    "gl-summary": gl_summary,
    "comparative-pl": comparative_pl,
    "check-overdue": check_overdue,
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

    # Common filters
    parser.add_argument("--company-id")
    parser.add_argument("--company", dest="company_name", default=None)  # NL: company by name
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--as-of-date")
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

    # Aging
    parser.add_argument("--customer-id")
    parser.add_argument("--supplier-id")
    parser.add_argument("--aging-buckets", default="30,60,90,120")

    # Budget
    parser.add_argument("--fiscal-year-id")

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
        ACTIONS[args.action](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
