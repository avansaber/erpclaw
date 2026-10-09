#!/usr/bin/env python3
"""ERPClaw Journals Skill — db_query.py

Journal entry CRUD with draft→submit→cancel lifecycle.
On submit, posts balanced GL entries via shared lib.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import calendar
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

# Add shared lib to path
try:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.db import get_connection, unexpected_error_message
    from erpclaw_lib.decimal_utils import to_decimal, round_currency
    from erpclaw_lib.validation import check_input_lengths
    from erpclaw_lib.gl_posting import (
        validate_gl_entries,
        insert_gl_entries,
        reverse_gl_entries,
        take_chain_heads,
    )
    from erpclaw_lib.cwip_posting import (
        get_under_construction_asset, cwip_debit_legs, record_cwip_accumulation,
        reverse_cwip_accumulations,
    )
    from erpclaw_lib.naming import get_next_name
    from erpclaw_lib.dimensions import (
        parse_dimension_input,
        validate_document_dimensions,
        dimensions_json_text,
    )
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
    from erpclaw_lib.query_helpers import resolve_company_id, resolve_scope_company
    from erpclaw_lib.query import Q, P, Table, Field, fn, Order, line_order, insert_row
    from erpclaw_lib import authority_gate
    from erpclaw_lib.authorization_consumption import INPUT_INVALID
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw first: clawhub install erpclaw", "suggestion": "clawhub install erpclaw"}))
    sys.exit(1)

REQUIRED_TABLES = ["company", "account"]

# PyPika table aliases
_t_je = Table("journal_entry")
_t_jel = Table("journal_entry_line")
_t_account = Table("account")
_t_company = Table("company")
_t_cost_center = Table("cost_center")
_t_rjt = Table("recurring_journal_template")
_t_fy = Table("fiscal_year")

VALID_ENTRY_TYPES = (
    "journal", "opening", "closing", "depreciation",
    "write_off", "exchange_rate_revaluation",
    "inter_company", "credit_note", "debit_note",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_paging(args, default_limit=20, default_offset=0):
    raw_limit = getattr(args, "limit", None)
    raw_offset = getattr(args, "offset", None)
    if raw_limit is None or (isinstance(raw_limit, str) and raw_limit.strip() == ""):
        limit = default_limit
    else:
        if isinstance(raw_limit, bool) or isinstance(raw_limit, float):
            err("--limit must be a positive integer")
        try:
            limit = int(raw_limit.strip()) if isinstance(raw_limit, str) else int(raw_limit)
        except (ValueError, TypeError):
            err("--limit must be a positive integer")
        if limit <= 0:
            err("--limit must be a positive integer")
    if raw_offset is None or (isinstance(raw_offset, str) and raw_offset.strip() == ""):
        offset = default_offset
    else:
        if isinstance(raw_offset, bool) or isinstance(raw_offset, float):
            err("--offset must be a non-negative integer")
        try:
            offset = int(raw_offset.strip()) if isinstance(raw_offset, str) else int(raw_offset)
        except (ValueError, TypeError):
            err("--offset must be a non-negative integer")
        if offset < 0:
            err("--offset must be a non-negative integer")
    return limit, offset


def _validate_lines(lines: list[dict]) -> tuple[Decimal, Decimal]:
    """Validate journal entry lines. Returns (total_debit, total_credit).

    Raises ValueError on validation failure.
    """
    if len(lines) < 2:
        raise ValueError("At least 2 lines are required")

    total_debit = Decimal("0")
    total_credit = Decimal("0")

    for i, line in enumerate(lines):
        if "account_id" not in line or not line["account_id"]:
            raise ValueError(f"Line {i+1}: account_id is required")

        debit = to_decimal(line.get("debit", "0"))
        credit = to_decimal(line.get("credit", "0"))

        if debit < 0 or credit < 0:
            raise ValueError(f"Line {i+1}: debit and credit must be >= 0")

        if debit > 0 and credit > 0:
            raise ValueError(f"Line {i+1}: cannot have both debit and credit > 0")

        if debit == 0 and credit == 0:
            raise ValueError(f"Line {i+1}: either debit or credit must be > 0")

        total_debit += debit
        total_credit += credit

    total_debit = round_currency(total_debit)
    total_credit = round_currency(total_credit)

    if total_debit != total_credit:
        raise ValueError(
            f"Total debit ({total_debit}) must equal total credit ({total_credit})"
        )

    return total_debit, total_credit


def _parse_header_dimensions(args):
    """Parse --dimensions/--dimension-key/--dimension-value into a dict or None.

    Returns None when the caller supplied no dimension input at all (so the
    caller can tell "leave it" from "clear it" with an explicit '{}').
    A ValueError from the shared parser ends the action with err(), before
    any write.
    """
    try:
        return parse_dimension_input(
            getattr(args, "dimensions", None),
            getattr(args, "dimension_key", None),
            getattr(args, "dimension_value", None))
    except ValueError as e:
        err(str(e))


def _parse_line_dimensions(lines: list[dict]) -> list[dict]:
    """Normalise each line's "dimensions" object in place; refuse bad ones.

    A present non-object value is refused as
    "Line {n}: dimensions must be a JSON object"; each object goes through
    the same key/value rules as the header so the texts are identical,
    prefixed "Line {n}: ".
    """
    for i, line in enumerate(lines):
        if "dimensions" not in line:
            continue
        raw = line["dimensions"]
        if not isinstance(raw, dict):
            err(f"Line {i+1}: dimensions must be a JSON object")
        try:
            line["dimensions"] = parse_dimension_input(
                json.dumps(raw), None, None)
        except ValueError as e:
            err(f"Line {i+1}: {e}")
    return lines


def _loads_dims(text):
    """Parse a stored dimensions_json value back to a dict (never fail)."""
    try:
        parsed = json.loads(text) if isinstance(text, str) else text
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validate_effective_dimensions(conn, header_obj, lines):
    """Check each line's effective object (header merged with line, line wins).

    A shared-registry refusal ends the action with err() and nothing written.
    """
    header_obj = header_obj or {}
    for line in lines:
        line_dims = line.get("dimensions") or {}
        if not isinstance(line_dims, dict):
            line_dims = {}
        effective = dict(header_obj)
        effective.update(line_dims)
        try:
            validate_document_dimensions(
                conn, effective, account_ids=[line.get("account_id")])
        except ValueError as e:
            err(str(e))


def _insert_lines(conn, journal_entry_id: str, lines: list[dict]):
    """Insert journal_entry_line rows."""
    for line in lines:
        line_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO journal_entry_line
               (id, journal_entry_id, account_id, party_type, party_id,
                debit, credit, cost_center_id, project_id, remark,
                dimensions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (line_id, journal_entry_id,
             line["account_id"],
             line.get("party_type"),
             line.get("party_id"),
             str(round_currency(to_decimal(line.get("debit", "0")))),
             str(round_currency(to_decimal(line.get("credit", "0")))),
             line.get("cost_center_id"),
             line.get("project_id"),
             line.get("remark"),
             dimensions_json_text(line.get("dimensions"))),
        )


def _get_je_or_err(conn, journal_entry_id: str) -> dict:
    """Fetch a journal entry by ID. Calls err() if not found."""
    q = Q.from_(_t_je).select(_t_je.star).where(_t_je.id == P())
    row = conn.execute(q.get_sql(), (journal_entry_id,)).fetchone()
    if not row:
        err(f"Journal entry {journal_entry_id} not found")
    return row_to_dict(row)


def _get_je_lines(conn, journal_entry_id: str) -> list[dict]:
    """Fetch journal entry lines with account name join."""
    jel = Table("journal_entry_line")
    a = Table("account")
    q = (Q.from_(jel)
         .select(jel.star, a.name.as_("account_name"))
         .join(a).on(a.id == jel.account_id)
         .where(jel.journal_entry_id == P())
         .orderby(line_order(jel)))
    rows = conn.execute(q.get_sql(), (journal_entry_id,)).fetchall()
    return [row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 1. add-journal-entry
# ---------------------------------------------------------------------------

def create_expense_allocation(conn, args):
    """Prepare a balanced, explicitly weighted internal expense recharge."""
    company_id = getattr(args, "company_id", None)
    source_account = getattr(args, "source_account_id", None)
    source_center = getattr(args, "source_cost_center_id", None)
    if not all(isinstance(value, str) and value for value in (company_id, source_account, source_center)):
        err("--company-id, --source-account-id and --source-cost-center-id are required")
    if any(getattr(args, name, None) for name in ("dimensions", "dimension_key", "dimension_value")):
        err("Expense allocation sets its cost-centre tags; other dimension arguments are not supported")
    posting_date = getattr(args, "posting_date", None)
    try:
        if date.fromisoformat(posting_date).isoformat() != posting_date:
            raise ValueError
    except (TypeError, ValueError):
        err("--posting-date must be an ISO date")
    raw = getattr(args, "amount", None)
    try:
        if isinstance(raw, (bool, float)):
            raise ValueError
        amount = Decimal(str(raw))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000000"):
            raise ValueError
        amount = round_currency(amount)
        if amount <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        err("--amount must be positive and finite, up to 1000000000000, and round to at least one cent")
    try:
        targets = json.loads(getattr(args, "allocations", None))
    except (TypeError, ValueError):
        err("--allocations must be a JSON array")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
        err("--allocations requires between one and 100 targets")

    def validate_account(account_id):
        query = (Q.from_(_t_account).select(_t_account.id).where(_t_account.id == P())
                 .where(_t_account.company_id == P()).where(_t_account.root_type == "expense")
                 .where(_t_account.is_group == 0).where(_t_account.is_frozen == 0))
        if not conn.execute(query.get_sql(), (account_id, company_id)).fetchone():
            err("Every allocation account must be an unfrozen leaf expense account belonging to --company-id")

    def validate_center(center_id):
        query = (Q.from_(_t_cost_center).select(_t_cost_center.id)
                 .where(_t_cost_center.id == P()).where(_t_cost_center.company_id == P())
                 .where(_t_cost_center.is_group == 0))
        if not conn.execute(query.get_sql(), (center_id, company_id)).fetchone():
            err("Every allocation cost centre must be an owned leaf")

    validate_account(source_account)
    validate_center(source_center)
    prepared, seen, total = [], set(), Decimal("0")
    for target in targets:
        if not isinstance(target, dict) or set(target) - {"account_id", "cost_center_id", "percentage"}:
            err("Each allocation needs cost_center_id and percentage, with optional account_id")
        center_id = target.get("cost_center_id")
        if not isinstance(center_id, str) or not center_id or center_id == source_center or center_id in seen:
            err("Targets must name distinct cost centres different from the source")
        seen.add(center_id)
        validate_center(center_id)
        account_id = target.get("account_id", source_account)
        if not isinstance(account_id, str) or not account_id:
            err("Target account_id must be a nonempty id")
        validate_account(account_id)
        raw_percent = target.get("percentage")
        try:
            if isinstance(raw_percent, (bool, float)):
                raise ValueError
            percent = Decimal(str(raw_percent))
            if (not percent.is_finite() or percent <= 0 or percent > 100
                    or percent != percent.quantize(Decimal("0.000001"))):
                raise ValueError
        except (ValueError, InvalidOperation):
            err("Percentages must be positive exact values up to 100 with at most six decimal places")
        dims = {"cost_center": center_id}
        prepared.append((account_id, center_id, percent, dims))
        total += percent
    if total != Decimal("100"):
        err("Allocation percentages must total exactly 100")
    cents = amount * 100
    portions = [cents * target[2] / 100 for target in prepared]
    allocated = [int(value.to_integral_value(rounding=ROUND_DOWN)) for value in portions]
    remaining = int(cents) - sum(allocated)
    ranking = sorted(range(len(portions)), key=lambda i: (-(portions[i] - allocated[i]), i))
    for index in ranking[:remaining]:
        allocated[index] += 1
    lines = [{"account_id": source_account, "debit": "0.00", "credit": str(amount),
              "cost_center_id": source_center, "dimensions": {"cost_center": source_center}}]
    for (account_id, center_id, _, dims), share in zip(prepared, allocated):
        if share:
            lines.append({"account_id": account_id, "debit": str(round_currency(Decimal(share) / 100)),
                          "credit": "0.00", "cost_center_id": center_id, "dimensions": dims})
    draft_args = argparse.Namespace(**vars(args))
    draft_args.entry_type = "journal"
    draft_args.cwip_asset_id = None
    draft_args.remark = getattr(args, "remark", None) or "Internal expense allocation"
    draft_args.lines = json.dumps(lines)
    add_journal_entry(conn, draft_args)


def add_interfund_transfer(conn, args):
    """Prepare a reciprocal transfer draft balanced within both registered funds."""
    company_id = getattr(args, "company_id", None)
    fund_key = getattr(args, "fund_dimension", None) or "fund"
    source = getattr(args, "from_fund", None)
    target = getattr(args, "to_fund", None)
    if not company_id or not isinstance(source, str) or not isinstance(target, str):
        err("--company-id, --from-fund and --to-fund are required")
    if source != source.strip() or target != target.strip() or not source or not target or source == target:
        err("Use two distinct non-empty registered fund values without surrounding spaces")
    if getattr(args, "lines", None) or getattr(args, "cwip_asset_id", None) or (
            getattr(args, "entry_type", None) not in (None, "journal")):
        err("Interfund drafts construct their own journal lines and cannot capitalise an asset")
    raw = getattr(args, "amount", None)
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,2})?", raw):
        err("--amount must be positive decimal text with at most two fractional digits")
    amount = Decimal(raw)
    if amount <= 0:
        err("--amount must be positive")
    posting = getattr(args, "posting_date", None)
    try:
        if date.fromisoformat(posting).isoformat() != posting:
            raise ValueError()
    except (TypeError, ValueError):
        err("--posting-date must be YYYY-MM-DD")
    registry = Table("dimension_registry")
    row = conn.execute(Q.from_(registry).select(
        registry.is_active, registry.data_type, registry.allowed_values_json
    ).where(registry.key == P()).get_sql(), (fund_key,)).fetchone()
    try:
        allowed = json.loads(row["allowed_values_json"]) if row else None
    except (TypeError, ValueError):
        allowed = None
    if not row or not row["is_active"] or row["data_type"] != "enum" or (
            not isinstance(allowed, list) or source not in allowed or target not in allowed):
        err("Both funds must belong to the selected active registered enum dimension")
    header = _parse_header_dimensions(args) or {}
    if fund_key in header:
        err("Give fund values with --from-fund and --to-fund, not header dimensions")
    specs = (("source_cash_account_id", "asset", True),
             ("target_cash_account_id", "asset", True),
             ("due_from_account_id", "asset", False),
             ("due_to_account_id", "liability", False))
    ids = {}
    for field, root, cash in specs:
        account_id = getattr(args, field, None)
        account = conn.execute(Q.from_(_t_account).select(
            _t_account.company_id, _t_account.root_type, _t_account.account_type,
            _t_account.is_group, _t_account.disabled, _t_account.is_frozen
        ).where(_t_account.id == P()).get_sql(), (account_id,)).fetchone()
        if not account or account["company_id"] != company_id or account["root_type"] != root or (
                account["is_group"] or account["disabled"] or account["is_frozen"]):
            err(f"--{field.replace('_', '-')} must be an enabled, unfrozen {root} leaf of this company")
        if cash != (account["account_type"] in ("cash", "bank")):
            err("Cash legs require cash or bank accounts; due accounts must not be cash accounts")
        ids[field] = account_id
    if len({ids["due_from_account_id"], ids["due_to_account_id"],
            ids["source_cash_account_id"]}) < 3 or (
            ids["target_cash_account_id"] in (ids["due_from_account_id"], ids["due_to_account_id"])):
        err("Due-to and due-from accounts must differ from each other and the cash accounts")
    text = format(amount, ".2f")
    lines = [
        {"account_id": ids["source_cash_account_id"], "debit": "0.00", "credit": text,
         "dimensions": {fund_key: source}},
        {"account_id": ids["due_from_account_id"], "debit": text, "credit": "0.00",
         "dimensions": {fund_key: source}},
        {"account_id": ids["target_cash_account_id"], "debit": text, "credit": "0.00",
         "dimensions": {fund_key: target}},
        {"account_id": ids["due_to_account_id"], "debit": "0.00", "credit": text,
         "dimensions": {fund_key: target}},
    ]
    forwarded = argparse.Namespace(**vars(args))
    forwarded.lines = json.dumps(lines)
    forwarded.entry_type = "journal"
    forwarded.cwip_asset_id = None
    forwarded.remark = f"Interfund reciprocal transfer {source} to {target}; {getattr(args, 'remark', None) or ''}"
    try:
        add_journal_entry(conn, forwarded)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def add_journal_entry(conn, args):
    """Create a new draft journal entry with lines."""
    company_id = args.company_id
    if not company_id:
        err("--company-id is required")
    posting_date = args.posting_date
    if not posting_date:
        err("--posting-date is required")
    entry_type = args.entry_type or "journal"
    if entry_type not in VALID_ENTRY_TYPES:
        err(f"Invalid entry type '{entry_type}'. Valid: {VALID_ENTRY_TYPES}")

    # Validate company exists
    q = Q.from_(_t_company).select(_t_company.id).where(_t_company.id == P())
    company = conn.execute(q.get_sql(), (company_id,)).fetchone()
    if not company:
        err(f"Company {company_id} not found")

    # S3 CWIP hook (AVA-43): a --cwip-asset-id JE capitalises cost to a
    # construction-in-progress asset. Validate the asset is under_construction up
    # front; submit records the accumulation against the JE's CWIP debit leg in-tx.
    cwip_asset_id = getattr(args, "cwip_asset_id", None)
    if cwip_asset_id:
        try:
            get_under_construction_asset(conn, cwip_asset_id)
        except ValueError as e:
            err(str(e))

    # Parse lines
    lines_json = args.lines
    if not lines_json:
        err("--lines is required (JSON array)")
    try:
        lines = json.loads(lines_json) if isinstance(lines_json, str) else lines_json
    except json.JSONDecodeError as e:
        err("Invalid JSON format in --lines")

    # Validate lines
    try:
        total_debit, total_credit = _validate_lines(lines)
    except ValueError as e:
        err(str(e))

    # Validate all account_ids exist
    q_acct = Q.from_(_t_account).select(_t_account.id, _t_account.is_frozen).where(_t_account.id == P())
    for i, line in enumerate(lines):
        acct = conn.execute(q_acct.get_sql(), (line["account_id"],)).fetchone()
        if not acct:
            err(f"Line {i+1}: account {line['account_id']} not found")

    # Accounting dimensions (M6): header input plus per-line objects are
    # parsed and checked against the registry here, before get_next_name
    # consumes a naming-series step.
    header_dims = _parse_header_dimensions(args)
    header_obj = header_dims if header_dims is not None else {}
    _parse_line_dimensions(lines)
    _validate_effective_dimensions(conn, header_obj, lines)

    je_id = str(uuid.uuid4())
    naming = get_next_name(conn, "journal_entry", company_id=company_id)

    conn.execute(
        """INSERT INTO journal_entry
           (id, naming_series, posting_date, entry_type, total_debit, total_credit,
            remark, status, cwip_asset_id, company_id, dimensions_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)""",
        (je_id, naming, posting_date, entry_type,
         str(total_debit), str(total_credit),
         args.remark, cwip_asset_id, company_id,
         dimensions_json_text(header_obj)),
    )

    _insert_lines(conn, je_id, lines)

    audit(conn, "erpclaw-journals", "add-journal-entry", "journal_entry", je_id,
           new_values={"naming_series": naming, "entry_type": entry_type,
                       "posting_date": posting_date, "lines": len(lines)})
    conn.commit()

    ok({"status": "created", "journal_entry_id": je_id,
         "naming_series": naming})


# ---------------------------------------------------------------------------
# 2. update-journal-entry
# ---------------------------------------------------------------------------

def update_journal_entry(conn, args):
    """Update a draft journal entry. Only drafts can be updated."""
    je_id = args.journal_entry_id
    if not je_id:
        err("--journal-entry-id is required")

    je = _get_je_or_err(conn, je_id)
    if je["status"] != "draft":
        err(f"Cannot update: journal entry is '{je['status']}' (must be 'draft')",
             suggestion="Cancel the document first, then make changes.")

    updated_fields = []
    old_values = {}

    # Accounting dimensions (M6): parse the header input and --lines here,
    # before the first UPDATE below, so a refusal writes nothing and consumes
    # no naming-series step. Validation only runs when the caller supplied
    # header input or --lines.
    header_input = _parse_header_dimensions(args)
    replacement_lines = None
    replacement_totals = None
    if args.lines:
        try:
            replacement_lines = (json.loads(args.lines)
                                 if isinstance(args.lines, str) else args.lines)
        except json.JSONDecodeError as e:
            err("Invalid JSON format in --lines")
        try:
            replacement_totals = _validate_lines(replacement_lines)
        except ValueError as e:
            err(str(e))
        q_acct_pre = Q.from_(_t_account).select(_t_account.id).where(_t_account.id == P())
        for i, line in enumerate(replacement_lines):
            acct = conn.execute(q_acct_pre.get_sql(), (line["account_id"],)).fetchone()
            if not acct:
                err(f"Line {i+1}: account {line['account_id']} not found")
        _parse_line_dimensions(replacement_lines)
    if header_input is not None or args.lines:
        if header_input is not None:
            validation_header = header_input
        else:
            validation_header = _loads_dims(je.get("dimensions_json"))
        if replacement_lines is not None:
            validation_lines = replacement_lines
        else:
            stored = _get_je_lines(conn, je_id)
            validation_lines = [
                {"account_id": l["account_id"],
                 "dimensions": _loads_dims(l.get("dimensions_json"))}
                for l in stored]
        _validate_effective_dimensions(conn, validation_header, validation_lines)

    # Update posting_date
    if args.posting_date:
        old_values["posting_date"] = je["posting_date"]
        conn.execute("UPDATE journal_entry SET posting_date = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.posting_date, je_id))
        updated_fields.append("posting_date")

    # Update entry_type
    if args.entry_type:
        if args.entry_type not in VALID_ENTRY_TYPES:
            err(f"Invalid entry type '{args.entry_type}'. Valid: {VALID_ENTRY_TYPES}")
        old_values["entry_type"] = je["entry_type"]
        conn.execute("UPDATE journal_entry SET entry_type = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.entry_type, je_id))
        updated_fields.append("entry_type")

    # Update remark
    if args.remark is not None:
        old_values["remark"] = je["remark"]
        conn.execute("UPDATE journal_entry SET remark = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.remark, je_id))
        updated_fields.append("remark")

    # Replace header dimensions when dimension input was supplied (an
    # explicit --dimensions '{}' clears the stored header object).
    new_header_text = None
    if header_input is not None:
        old_values["dimensions_json"] = je.get("dimensions_json") or "{}"
        new_header_text = dimensions_json_text(header_input)
        conn.execute("UPDATE journal_entry SET dimensions_json = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (new_header_text, je_id))
        updated_fields.append("dimensions")

    # Replace lines if provided (parsed and dimension-checked above)
    if replacement_lines is not None:
        lines = replacement_lines
        total_debit, total_credit = replacement_totals

        # Delete old lines, insert new
        q_del = Q.from_(_t_jel).delete().where(_t_jel.journal_entry_id == P())
        conn.execute(q_del.get_sql(), (je_id,))
        _insert_lines(conn, je_id, lines)

        conn.execute(
            """UPDATE journal_entry SET total_debit = ?, total_credit = ?,
               updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?""",
            (str(total_debit), str(total_credit), je_id),
        )
        updated_fields.append("lines")

    if not updated_fields:
        err("No fields to update")

    new_values = {"updated_fields": updated_fields}
    if new_header_text is not None:
        new_values["dimensions_json"] = new_header_text
    audit(conn, "erpclaw-journals", "update-journal-entry", "journal_entry", je_id,
           old_values=old_values,
           new_values=new_values)
    conn.commit()

    ok({"status": "updated", "journal_entry_id": je_id,
         "updated_fields": updated_fields})


# ---------------------------------------------------------------------------
# 3. get-journal-entry
# ---------------------------------------------------------------------------

def get_journal_entry(conn, args):
    """Get a journal entry with all its lines."""
    je_id = args.journal_entry_id
    if not je_id:
        err("--journal-entry-id is required")

    scope_company_id = None
    if getattr(args, "company_id", None) or getattr(args, "company_name", None):
        scope_company_id = resolve_scope_company(
            conn, getattr(args, "company_id", None),
            getattr(args, "company_name", None))

    je = _get_je_or_err(conn, je_id)
    if scope_company_id is not None and je["company_id"] != scope_company_id:
        err(f"Journal entry {je_id} belongs to another company")
    lines = _get_je_lines(conn, je_id)

    # Format lines for output
    formatted_lines = []
    for line in lines:
        formatted_lines.append({
            "id": line["id"],
            "account_id": line["account_id"],
            "account_name": line["account_name"],
            "debit": line["debit"],
            "credit": line["credit"],
            "party_type": line.get("party_type"),
            "party_id": line.get("party_id"),
            "cost_center_id": line.get("cost_center_id"),
            "project_id": line.get("project_id"),
            "remark": line.get("remark"),
            "dimensions_json": line.get("dimensions_json") or "{}",
        })

    ok({
        "id": je["id"],
        "naming_series": je["naming_series"],
        "dimensions_json": je.get("dimensions_json") or "{}",
        "posting_date": je["posting_date"],
        "entry_type": je["entry_type"],
        "document_status": je["status"],
        "total_debit": je["total_debit"],
        "total_credit": je["total_credit"],
        "remark": je.get("remark"),
        "amended_from": je.get("amended_from"),
        "company_id": je["company_id"],
        "lines": formatted_lines,
    })


# ---------------------------------------------------------------------------
# 4. list-journal-entries
# ---------------------------------------------------------------------------

def list_journal_entries(conn, args):
    """List journal entries with filtering."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    je = Table("journal_entry")
    params = [company_id]

    # Build base query with required company filter
    base = Q.from_(je).where(je.company_id == P())

    if args.je_status:
        base = base.where(je.status == P())
        params.append(args.je_status)

    if args.entry_type:
        base = base.where(je.entry_type == P())
        params.append(args.entry_type)

    if args.from_date:
        base = base.where(je.posting_date >= P())
        params.append(args.from_date)

    if args.to_date:
        base = base.where(je.posting_date <= P())
        params.append(args.to_date)

    if args.account_id:
        # Subquery: keep as raw SQL snippet via Criterion.any for clarity
        jel = Table("journal_entry_line")
        sub = Q.from_(jel).select(jel.journal_entry_id).where(jel.account_id == P())
        base = base.where(je.id.isin(sub))
        params.append(args.account_id)

    # Total count
    q_count = base.select(fn.Count("*"))
    count_row = conn.execute(q_count.get_sql(), params).fetchone()
    total_count = count_row[0]

    # Paginated results
    limit, offset = _parse_paging(args)
    list_params = params + [limit, offset]

    q_list = (base.select(
                  je.id, je.naming_series, je.posting_date, je.entry_type,
                  je.status, je.total_debit, je.total_credit, je.remark)
              .orderby(je.posting_date, order=Order.desc)
              .orderby(je.created_at, order=Order.desc)
              .limit(P()).offset(P()))
    rows = conn.execute(q_list.get_sql(), list_params).fetchall()

    entries = [row_to_dict(r) for r in rows]
    ok({"entries": entries, "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 5. submit-journal-entry
# ---------------------------------------------------------------------------

def submit_journal_entry(conn, args):
    """Submit a draft JE: re-validate, post GL entries, update status."""
    je_id = args.journal_entry_id
    if not je_id:
        err("--journal-entry-id is required")

    je = _get_je_or_err(conn, je_id)
    if je["status"] != "draft":
        err(f"Cannot submit: journal entry is '{je['status']}' (must be 'draft')")

    try:
        take_chain_heads(conn, [je["company_id"]])
        _rr_q = Q.from_(_t_je).select(_t_je.star).where(_t_je.id == P())
        _rr_row = conn.execute(_rr_q.get_sql(), (je_id,)).fetchone()
        if not _rr_row:
            conn.rollback()
            err(f"Journal entry {je_id} not found")
        je = row_to_dict(_rr_row)
        if je["status"] != "draft":
            conn.rollback()
            err(f"Cannot submit: journal entry is '{je['status']}' (must be 'draft')")

        lines = _get_je_lines(conn, je_id)

        # Re-validate lines (they were validated at creation but re-check)
        try:
            _validate_lines([{
                "account_id": l["account_id"],
                "debit": l["debit"],
                "credit": l["credit"],
            } for l in lines])
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-journals] {e}\n")
            err("Validation failed at submit")

        # Build GL entries from lines. Each ledger dict carries the line's
        # effective dimensions (stored header merged with the stored line object,
        # line wins per key); untagged lines post exactly the dict they always did.
        submit_header = _loads_dims(je.get("dimensions_json"))
        gl_entries = []
        for line in lines:
            line_dims = _loads_dims(line.get("dimensions_json"))
            effective = dict(submit_header)
            effective.update(line_dims)
            # Line wins: a line's own cost center replaces a header-carried
            # cost_center tag in that leg's dimensions before posting, so step
            # 13 sees one cost center on the leg.
            if (line.get("cost_center_id") and "cost_center" not in line_dims
                    and effective.get("cost_center")
                    and effective["cost_center"] != line["cost_center_id"]):
                effective["cost_center"] = line["cost_center_id"]
            gl_entry = {
                "account_id": line["account_id"],
                "debit": line["debit"],
                "credit": line["credit"],
                "party_type": line.get("party_type"),
                "party_id": line.get("party_id"),
                "cost_center_id": line.get("cost_center_id"),
            }
            if effective:
                gl_entry["dimensions"] = effective
            gl_entries.append(gl_entry)

        # Registry rules can change while a journal remains a draft. Check the
        # effective posting tags again on the locked submit connection.
        _validate_effective_dimensions(conn, {}, gl_entries)

        # Single transaction: validate GL, insert GL entries, update JE status
        try:
            is_opening = je["entry_type"] in ("opening",)
            validate_gl_entries(
                conn, gl_entries, je["company_id"],
                je["posting_date"], is_opening=is_opening,
                voucher_type="journal_entry",
            )
            gl_ids = insert_gl_entries(
                conn, gl_entries,
                voucher_type="journal_entry",
                voucher_id=je_id,
                posting_date=je["posting_date"],
                company_id=je["company_id"],
                remarks=je.get("remark") or "",
                is_opening=is_opening,
                # S3 CWIP hook (AVA-43): a --cwip-asset-id JE is the sanctioned
                # capitalization path, so its CWIP debit leg is permitted.
                allow_cwip=bool(je.get("cwip_asset_id")),
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-journals] {e}\n")
            err(f"GL posting failed: {e}")

        # S3 CWIP hook (AVA-43): a JE tagged with --cwip-asset-id must debit a
        # capital_work_in_progress account; record the accumulation against that leg
        # in THIS submit transaction. gl_ids is 1:1 with gl_entries by index.
        cwip_accum_id = None
        cwip_asset_id = je.get("cwip_asset_id")
        if cwip_asset_id:
            try:
                cwip_asset = get_under_construction_asset(conn, cwip_asset_id)
                legs = cwip_debit_legs(conn, gl_entries)
                if not legs:
                    raise ValueError(
                        "A journal entry tagged with --cwip-asset-id must debit a "
                        "capital_work_in_progress account.")
                if len({a for _, a, _ in legs}) > 1:
                    raise ValueError(
                        "Journal entry debits multiple capital_work_in_progress "
                        "accounts; one CWIP account per asset.")
                cwip_amount = sum((d for _, _, d in legs), Decimal("0"))
                cwip_accum_id = record_cwip_accumulation(
                    conn, cwip_asset, cwip_amount,
                    source_voucher_type="journal_entry", source_voucher_id=je_id,
                    gl_entry_id=gl_ids[legs[0][0]], accumulated_at=je["posting_date"],
                    notes=je.get("remark") or f"Journal entry {je.get('naming_series') or je_id}")
            except ValueError as e:
                sys.stderr.write(f"[erpclaw-journals] {e}\n")
                err(f"CWIP accumulation failed: {e}")

        _cas = conn.execute(
            """UPDATE journal_entry SET status = 'submitted',
               updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ? AND status = ?""",
            (je_id, "draft"),
        )
        if _cas.rowcount == 0:
            conn.rollback()
            _fr_row = conn.execute(_rr_q.get_sql(), (je_id,)).fetchone()
            if not _fr_row:
                err(f"Journal entry {je_id} not found")
            _fresh = row_to_dict(_fr_row)
            err(f"Cannot submit: journal entry is '{_fresh['status']}' (must be 'draft')")

        audit(conn, "erpclaw-journals", "submit-journal-entry", "journal_entry", je_id,
               new_values={"gl_entries_created": len(gl_ids)})
        conn.commit()
    except SystemExit:
        conn.rollback()
        raise

    resp = {"status": "submitted", "journal_entry_id": je_id,
            "gl_entries_created": len(gl_ids)}
    if cwip_accum_id:
        resp["cwip_asset_id"] = cwip_asset_id
        resp["cwip_accumulation_id"] = cwip_accum_id
    ok(resp)


# ---------------------------------------------------------------------------
# 6. cancel-journal-entry
# ---------------------------------------------------------------------------


def cancel_journal_entry(conn, args):
    """Cancel a submitted JE: reverse GL entries, update status."""
    je_id = args.journal_entry_id
    if not je_id:
        err("--journal-entry-id is required")

    je = _get_je_or_err(conn, je_id)
    if je["status"] != "submitted":
        err(f"Cannot cancel: journal entry is '{je['status']}' (must be 'submitted')")

    try:
        take_chain_heads(conn, [je["company_id"]])
        _rr_q = Q.from_(_t_je).select(_t_je.star).where(_t_je.id == P())
        _rr_row = conn.execute(_rr_q.get_sql(), (je_id,)).fetchone()
        if not _rr_row:
            conn.rollback()
            err(f"Journal entry {je_id} not found")
        je = row_to_dict(_rr_row)
        if je["status"] != "submitted":
            conn.rollback()
            err(f"Cannot cancel: journal entry is '{je['status']}' (must be 'submitted')")

        # Single transaction: reverse GL entries + update status
        try:
            reversal_ids = reverse_gl_entries(
                conn,
                voucher_type="journal_entry",
                voucher_id=je_id,
                posting_date=je["posting_date"],
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-journals] {e}\n")
            err(f"GL reversal failed: {e}")

        # S3 CWIP hook (AVA-43): if this JE accumulated cost to a CWIP asset, unwind the
        # accumulation row + asset carrying value (the GL CWIP leg was just reversed).
        if je.get("cwip_asset_id"):
            reverse_cwip_accumulations(conn, "journal_entry", je_id)

        _cas = conn.execute(
            """UPDATE journal_entry SET status = 'cancelled',
               updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ? AND status = ?""",
            (je_id, "submitted"),
        )
        if _cas.rowcount == 0:
            conn.rollback()
            _fr_row = conn.execute(_rr_q.get_sql(), (je_id,)).fetchone()
            if not _fr_row:
                err(f"Journal entry {je_id} not found")
            _fresh = row_to_dict(_fr_row)
            err(f"Cannot cancel: journal entry is '{_fresh['status']}' (must be 'submitted')")

        audit(conn, "erpclaw-journals", "cancel-journal-entry", "journal_entry", je_id,
               new_values={"reversed_gl_entries": len(reversal_ids)})
        conn.commit()
    except SystemExit:
        conn.rollback()
        raise

    ok({"status": "cancelled", "journal_entry_id": je_id, "reversed": True})


# ---------------------------------------------------------------------------
# 7. amend-journal-entry
# ---------------------------------------------------------------------------


def amend_journal_entry(conn, args):
    """Amend a submitted JE: cancel old, create new linked draft."""
    je_id = args.journal_entry_id
    if not je_id:
        err("--journal-entry-id is required")

    je = _get_je_or_err(conn, je_id)
    if je["status"] != "submitted":
        err(f"Cannot amend: journal entry is '{je['status']}' (must be 'submitted')")

    # Accounting dimensions (M6): parse the header input and --lines here,
    # before reverse_gl_entries writes anything, so a refusal writes nothing
    # and consumes no naming-series step. An amend that supplies header input
    # or --lines uses what it is given; otherwise both are copied over.
    amend_header_input = _parse_header_dimensions(args)
    if args.lines:
        try:
            amend_lines = (json.loads(args.lines)
                           if isinstance(args.lines, str) else args.lines)
        except json.JSONDecodeError as e:
            err("Invalid JSON format in --lines")
    else:
        q_amend_lines = Q.from_(_t_jel).select(_t_jel.star).where(_t_jel.journal_entry_id == P())
        amend_old_lines = conn.execute(q_amend_lines.get_sql(), (je_id,)).fetchall()
        amend_lines = []
        for ol in amend_old_lines:
            old_dict = row_to_dict(ol)
            amend_lines.append({
                "account_id": old_dict["account_id"],
                "debit": old_dict["debit"],
                "credit": old_dict["credit"],
                "party_type": old_dict.get("party_type"),
                "party_id": old_dict.get("party_id"),
                "cost_center_id": old_dict.get("cost_center_id"),
                "project_id": old_dict.get("project_id"),
                "remark": old_dict.get("remark"),
                "dimensions": _loads_dims(old_dict.get("dimensions_json")),
            })
    _parse_line_dimensions(amend_lines)
    if amend_header_input is not None:
        amend_header_obj = amend_header_input
    else:
        amend_header_obj = _loads_dims(je.get("dimensions_json"))
    _validate_effective_dimensions(conn, amend_header_obj, amend_lines)

    naming = get_next_name(conn, "journal_entry", company_id=je["company_id"])

    try:
        take_chain_heads(conn, [je["company_id"]])
        _rr_q = Q.from_(_t_je).select(_t_je.star).where(_t_je.id == P())
        _rr_row = conn.execute(_rr_q.get_sql(), (je_id,)).fetchone()
        if not _rr_row:
            conn.rollback()
            err(f"Journal entry {je_id} not found")
        je = row_to_dict(_rr_row)
        if je["status"] != "submitted":
            conn.rollback()
            err(f"Cannot amend: journal entry is '{je['status']}' (must be 'submitted')")

        # Cancel the old JE (reverse GL entries)
        try:
            reverse_gl_entries(
                conn,
                voucher_type="journal_entry",
                voucher_id=je_id,
                posting_date=je["posting_date"],
            )
        except ValueError as e:
            sys.stderr.write(f"[erpclaw-journals] {e}\n")
            err(f"GL reversal failed: {e}")

        _cas = conn.execute(
            """UPDATE journal_entry SET status = 'amended',
               updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ? AND status = ?""",
            (je_id, "submitted"),
        )
        if _cas.rowcount == 0:
            conn.rollback()
            _fr_row = conn.execute(_rr_q.get_sql(), (je_id,)).fetchone()
            if not _fr_row:
                err(f"Journal entry {je_id} not found")
            _fresh = row_to_dict(_fr_row)
            err(f"Cannot amend: journal entry is '{_fresh['status']}' (must be 'submitted')")

        # Lines were parsed and dimension-checked before the reversal above.
        new_lines = amend_lines

        # Validate lines
        try:
            total_debit, total_credit = _validate_lines(new_lines)
        except ValueError as e:
            err(str(e))

        # Create new draft JE
        new_je_id = str(uuid.uuid4())
        new_posting_date = args.posting_date or je["posting_date"]

        conn.execute(
            """INSERT INTO journal_entry
               (id, naming_series, posting_date, entry_type, total_debit, total_credit,
                remark, status, amended_from, company_id, dimensions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)""",
            (new_je_id, naming, new_posting_date, je["entry_type"],
             str(total_debit), str(total_credit),
             args.remark if args.remark is not None else je.get("remark"),
             je_id, je["company_id"],
             dimensions_json_text(amend_header_obj)),
        )

        _insert_lines(conn, new_je_id, new_lines)

        audit(conn, "erpclaw-journals", "amend-journal-entry", "journal_entry", je_id,
               new_values={"new_journal_entry_id": new_je_id, "new_naming_series": naming})
        conn.commit()
    except SystemExit:
        conn.rollback()
        raise

    ok({"status": "created", "original_id": je_id,
         "new_journal_entry_id": new_je_id,
         "new_naming_series": naming})


# ---------------------------------------------------------------------------
# 8. delete-journal-entry
# ---------------------------------------------------------------------------

def delete_journal_entry(conn, args):
    """Delete a draft JE. Only drafts can be deleted."""
    je_id = args.journal_entry_id
    if not je_id:
        err("--journal-entry-id is required")

    je = _get_je_or_err(conn, je_id)
    if je["status"] != "draft":
        err(f"Cannot delete: journal entry is '{je['status']}' (only 'draft' can be deleted)",
             suggestion="Cancel the document first, then delete.")

    naming = je["naming_series"]

    # Delete lines first (FK constraint), then header
    q_del_lines = Q.from_(_t_jel).delete().where(_t_jel.journal_entry_id == P())
    conn.execute(q_del_lines.get_sql(), (je_id,))
    q_del_je = Q.from_(_t_je).delete().where(_t_je.id == P())
    conn.execute(q_del_je.get_sql(), (je_id,))

    audit(conn, "erpclaw-journals", "delete-journal-entry", "journal_entry", je_id,
           old_values={"naming_series": naming})
    conn.commit()

    ok({"status": "deleted", "deleted": True})


# ---------------------------------------------------------------------------
# 9. duplicate-journal-entry
# ---------------------------------------------------------------------------

def duplicate_journal_entry(conn, args):
    """Duplicate a JE as a new draft. Copies all lines."""
    je_id = args.journal_entry_id
    if not je_id:
        err("--journal-entry-id is required")

    je = _get_je_or_err(conn, je_id)
    q_lines = Q.from_(_t_jel).select(_t_jel.star).where(_t_jel.journal_entry_id == P())
    old_lines = conn.execute(q_lines.get_sql(), (je_id,)).fetchall()

    new_lines = []
    for ol in old_lines:
        old_dict = row_to_dict(ol)
        new_lines.append({
            "account_id": old_dict["account_id"],
            "debit": old_dict["debit"],
            "credit": old_dict["credit"],
            "party_type": old_dict.get("party_type"),
            "party_id": old_dict.get("party_id"),
            "cost_center_id": old_dict.get("cost_center_id"),
            "project_id": old_dict.get("project_id"),
            "remark": old_dict.get("remark"),
            "dimensions": _loads_dims(old_dict.get("dimensions_json")),
        })

    # Accounting dimensions (M6): the copied header and line objects are
    # validated before get_next_name consumes a naming-series step.
    duplicate_header_input = _parse_header_dimensions(args)
    if duplicate_header_input is not None:
        duplicate_header_obj = duplicate_header_input
    else:
        duplicate_header_obj = _loads_dims(je.get("dimensions_json"))
    _parse_line_dimensions(new_lines)
    _validate_effective_dimensions(conn, duplicate_header_obj, new_lines)

    # Validate lines (should always pass since source was valid)
    try:
        total_debit, total_credit = _validate_lines(new_lines)
    except ValueError as e:
        err(str(e))

    new_je_id = str(uuid.uuid4())
    posting_date = args.posting_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    naming = get_next_name(conn, "journal_entry", company_id=je["company_id"])

    conn.execute(
        """INSERT INTO journal_entry
           (id, naming_series, posting_date, entry_type, total_debit, total_credit,
            remark, status, company_id, dimensions_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (new_je_id, naming, posting_date, je["entry_type"],
         str(total_debit), str(total_credit),
         je.get("remark"), je["company_id"],
         dimensions_json_text(duplicate_header_obj)),
    )

    _insert_lines(conn, new_je_id, new_lines)

    audit(conn, "erpclaw-journals", "duplicate-journal-entry", "journal_entry", je_id,
           new_values={"new_journal_entry_id": new_je_id, "naming_series": naming})
    conn.commit()

    ok({"status": "created", "new_journal_entry_id": new_je_id,
         "naming_series": naming})


# ---------------------------------------------------------------------------
# 10. create-intercompany-je
# ---------------------------------------------------------------------------

def _ensure_intercompany_account(conn, company_id, name, root_type, account_type):
    """Find or create an intercompany account for a company."""
    q = (Q.from_(_t_account).select(_t_account.id)
         .where(_t_account.name == P())
         .where(_t_account.company_id == P()))
    acct = conn.execute(q.get_sql(), (name, company_id)).fetchone()
    if acct:
        return acct["id"]

    acct_id = str(uuid.uuid4())
    balance_dir = "debit_normal" if root_type == "asset" else "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, root_type, account_type, currency,
           is_group, balance_direction, company_id, depth)
           VALUES (?, ?, ?, ?, 'USD', 0, ?, ?, 0)""",
        (acct_id, name, root_type, account_type, balance_dir, company_id),
    )
    return acct_id


def create_intercompany_je(conn, args):
    """Create paired intercompany journal entries between two companies.

    Source company: DR Intercompany Receivable / CR Revenue (or specified account)
    Target company: DR Expense (or specified account) / CR Intercompany Payable
    Both JEs reference each other via remark field.
    """
    source_company_id = args.source_company_id
    target_company_id = args.target_company_id
    amount_str = args.amount
    description = args.description or "Intercompany transaction"
    posting_date = args.posting_date

    if not source_company_id:
        err("--source-company-id is required")
    if not target_company_id:
        err("--target-company-id is required")
    if not amount_str:
        err("--amount is required")
    if not posting_date:
        err("--posting-date is required")
    if source_company_id == target_company_id:
        err("Source and target company must be different")

    amount = to_decimal(amount_str)
    if amount <= 0:
        err("Amount must be positive")

    # Validate both companies exist and share the same currency
    q_co = Q.from_(_t_company).select(_t_company.id, _t_company.default_currency).where(_t_company.id == P())
    src_co = conn.execute(q_co.get_sql(), (source_company_id,)).fetchone()
    tgt_co = conn.execute(q_co.get_sql(), (target_company_id,)).fetchone()
    if not src_co:
        err(f"Source company {source_company_id} not found")
    if not tgt_co:
        err(f"Target company {target_company_id} not found")
    if src_co["default_currency"] != tgt_co["default_currency"]:
        err("Intercompany JE between different currencies is not supported (v2)")

    # Accounting dimensions (M6): the header input is stored on both drafts.
    # Parsed here, before _ensure_intercompany_account can insert accounts,
    # so a refusal writes nothing and consumes no naming-series step.
    ic_header_input = _parse_header_dimensions(args)
    ic_header_obj = ic_header_input if ic_header_input is not None else {}
    ic_header_text = dimensions_json_text(ic_header_obj)

    q_rev = (Q.from_(_t_account).select(_t_account.id)
             .where(_t_account.account_type == "revenue")
             .where(_t_account.company_id == P())
             .where(_t_account.is_group == 0)
             .limit(1))
    src_revenue = conn.execute(q_rev.get_sql(), (source_company_id,)).fetchone()
    if not src_revenue:
        err("Source company has no revenue account")

    q_exp = (Q.from_(_t_account).select(_t_account.id)
             .where(_t_account.account_type.isin(["expense", "cost_of_goods_sold"]))
             .where(_t_account.company_id == P())
             .where(_t_account.is_group == 0)
             .limit(1))
    tgt_expense = conn.execute(q_exp.get_sql(), (target_company_id,)).fetchone()
    if not tgt_expense:
        err("Target company has no expense account")

    # Get cost centers for P&L entries
    q_cc = (Q.from_(_t_cost_center).select(_t_cost_center.id)
            .where(_t_cost_center.company_id == P())
            .where(_t_cost_center.is_group == 0)
            .limit(1))
    src_cc = conn.execute(q_cc.get_sql(), (source_company_id,)).fetchone()
    tgt_cc = conn.execute(q_cc.get_sql(), (target_company_id,)).fetchone()

    # The four lines this action builds carry no per-line objects, so each
    # effective object is the header. The P&L legs exist already and are
    # checked before any account is created; the intercompany legs are
    # checked right after they are ensured, before any naming-series step.
    _validate_effective_dimensions(
        conn, ic_header_obj,
        [{"account_id": src_revenue["id"]},
         {"account_id": tgt_expense["id"]}])

    # Ensure intercompany accounts exist in both companies
    src_ic_recv = _ensure_intercompany_account(
        conn, source_company_id, "Intercompany Receivable", "asset", "receivable")

    tgt_ic_pay = _ensure_intercompany_account(
        conn, target_company_id, "Intercompany Payable", "liability", "payable")

    _validate_effective_dimensions(
        conn, ic_header_obj,
        [{"account_id": src_ic_recv}, {"account_id": tgt_ic_pay}])

    amt = str(round_currency(amount))

    # Create Source JE: DR Intercompany Receivable / CR Revenue
    src_je_id = str(uuid.uuid4())
    src_naming = get_next_name(conn, "journal_entry", company_id=source_company_id)
    conn.execute(
        """INSERT INTO journal_entry
           (id, naming_series, posting_date, entry_type, total_debit, total_credit,
            remark, status, company_id, dimensions_json)
           VALUES (?, ?, ?, 'inter_company', ?, ?, ?, 'draft', ?, ?)""",
        (src_je_id, src_naming, posting_date, amt, amt, description, source_company_id,
         ic_header_text),
    )
    # Source lines
    for line_data in [
        {"account_id": src_ic_recv, "debit": amt, "credit": "0"},
        {"account_id": src_revenue["id"], "debit": "0", "credit": amt,
         "cost_center_id": src_cc["id"] if src_cc else None},
    ]:
        line_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO journal_entry_line
               (id, journal_entry_id, account_id, debit, credit, cost_center_id,
                dimensions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (line_id, src_je_id, line_data["account_id"],
             line_data["debit"], line_data["credit"],
             line_data.get("cost_center_id"), "{}"),
        )

    # Create Target JE: DR Expense / CR Intercompany Payable
    tgt_je_id = str(uuid.uuid4())
    tgt_naming = get_next_name(conn, "journal_entry", company_id=target_company_id)
    conn.execute(
        """INSERT INTO journal_entry
           (id, naming_series, posting_date, entry_type, total_debit, total_credit,
            remark, status, company_id, dimensions_json)
           VALUES (?, ?, ?, 'inter_company', ?, ?, ?, 'draft', ?, ?)""",
        (tgt_je_id, tgt_naming, posting_date, amt, amt, description, target_company_id,
         ic_header_text),
    )
    # Target lines
    for line_data in [
        {"account_id": tgt_expense["id"], "debit": amt, "credit": "0",
         "cost_center_id": tgt_cc["id"] if tgt_cc else None},
        {"account_id": tgt_ic_pay, "debit": "0", "credit": amt},
    ]:
        line_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO journal_entry_line
               (id, journal_entry_id, account_id, debit, credit, cost_center_id,
                dimensions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (line_id, tgt_je_id, line_data["account_id"],
             line_data["debit"], line_data["credit"],
             line_data.get("cost_center_id"), "{}"),
        )

    # Store cross-references in remark
    conn.execute(
        "UPDATE journal_entry SET remark = ? WHERE id = ?",
        (f"{description} | Paired with {tgt_naming} ({target_company_id})", src_je_id),
    )
    conn.execute(
        "UPDATE journal_entry SET remark = ? WHERE id = ?",
        (f"{description} | Paired with {src_naming} ({source_company_id})", tgt_je_id),
    )

    audit(conn, "erpclaw-journals", "create-intercompany-je", "journal_entry", src_je_id,
           new_values={"target_je_id": tgt_je_id, "amount": amt})
    conn.commit()

    ok({
        "source_je_id": src_je_id, "source_naming": src_naming,
        "target_je_id": tgt_je_id, "target_naming": tgt_naming,
        "amount": amt,
        "description": description,
    })


# ---------------------------------------------------------------------------
# Recurring Journal Template helpers
# ---------------------------------------------------------------------------

def _advance_date(d: date, frequency: str) -> date:
    """Advance a date by one period based on frequency.

    Uses stdlib only (no dateutil). Handles month-end edge cases.
    """
    if frequency == "daily":
        return d + timedelta(days=1)
    elif frequency == "weekly":
        return d + timedelta(weeks=1)
    elif frequency == "monthly":
        month = d.month + 1
        year = d.year
        if month > 12:
            month = 1
            year += 1
        # Clamp day to month's max (e.g. Jan 31 → Feb 28)
        import calendar
        max_day = calendar.monthrange(year, month)[1]
        day = min(d.day, max_day)
        return date(year, month, day)
    elif frequency == "quarterly":
        month = d.month + 3
        year = d.year
        while month > 12:
            month -= 12
            year += 1
        import calendar
        max_day = calendar.monthrange(year, month)[1]
        day = min(d.day, max_day)
        return date(year, month, day)
    elif frequency == "annual":
        import calendar
        year = d.year + 1
        max_day = calendar.monthrange(year, d.month)[1]
        day = min(d.day, max_day)
        return date(year, d.month, day)
    else:
        raise ValueError(f"Unknown frequency: {frequency}")


VALID_FREQUENCIES = ("daily", "weekly", "monthly", "quarterly", "annual")


def add_expense_schedule(conn, args):
    """Create monthly accrual or prepaid recognition drafts using recurring JEs."""
    company_id = getattr(args, "company_id", None)
    name = getattr(args, "template_name", None)
    kind = getattr(args, "schedule_kind", None)
    if not company_id or not isinstance(name, str) or not name.strip():
        err("--company-id and --template-name are required")
    if kind not in ("prepaid", "accrual"):
        err("--schedule-kind must be prepaid or accrual")
    if getattr(args, "auto_submit", None):
        err("Expense schedules generate drafts; submit each journal through the normal approval path")
    raw_amount = getattr(args, "amount", None)
    if not isinstance(raw_amount, str) or not re.fullmatch(
            r"[0-9]{1,18}(?:\.[0-9]{1,2})?", raw_amount):
        err("--amount must be positive decimal text with at most two fractional digits")
    total = Decimal(raw_amount)
    if total <= 0:
        err("--amount must be positive")
    raw_periods = getattr(args, "periods", None)
    if not isinstance(raw_periods, str) or not re.fullmatch(r"[1-9][0-9]{0,2}", raw_periods):
        err("--periods must be an integer from 1 to 120")
    periods = int(raw_periods)
    if periods > 120 or int(total * 100) < periods:
        err("Use 1 to 120 periods, with at least one cent in every period")
    start_text = getattr(args, "start_date", None)
    try:
        start = date.fromisoformat(start_text)
        if start.isoformat() != start_text:
            raise ValueError("noncanonical date")
        dates = []
        for offset in range(periods):
            month_index = start.year * 12 + start.month - 1 + offset
            year, month_zero = divmod(month_index, 12)
            month = month_zero + 1
            dates.append(date(year, month, min(start.day, calendar.monthrange(year, month)[1])))
    except (TypeError, ValueError, OverflowError):
        err("--start-date must be YYYY-MM-DD and the schedule must fit the supported calendar")

    company_q = Q.from_(_t_company).select(_t_company.id).where(_t_company.id == P())
    if not conn.execute(company_q.get_sql(), (company_id,)).fetchone():
        err("Company not found")
    expense_id = getattr(args, "expense_account_id", None)
    balance_id = getattr(args, "balance_account_id", None)
    expected_balance_root = "asset" if kind == "prepaid" else "liability"
    for account_id, root, flag in (
            (expense_id, "expense", "expense-account-id"),
            (balance_id, expected_balance_root, "balance-account-id")):
        account_q = Q.from_(_t_account).select(
            _t_account.company_id, _t_account.root_type, _t_account.is_group,
            _t_account.disabled, _t_account.is_frozen).where(_t_account.id == P())
        row = conn.execute(account_q.get_sql(), (account_id,)).fetchone()
        if (not row or row["company_id"] != company_id or row["root_type"] != root
                or row["is_group"] or row["disabled"] or row["is_frozen"]):
            err(f"--{flag} must be an enabled, unfrozen {root} leaf account of this company")
    if expense_id == balance_id:
        err("Expense and balance accounts must differ")
    dimensions = _parse_header_dimensions(args) or {}
    if "cost_center" in dimensions:
        center_q = (Q.from_(_t_cost_center).select(_t_cost_center.id)
                    .where(_t_cost_center.id == P())
                    .where(_t_cost_center.company_id == P())
                    .where(_t_cost_center.is_group == 0))
        if not conn.execute(center_q.get_sql(),
                            (dimensions["cost_center"], company_id)).fetchone():
            err("Cost centre must be a leaf centre of this company")
    prototype = [{"account_id": expense_id, "debit": "0.01", "credit": "0.00"},
                 {"account_id": balance_id, "debit": "0.00", "credit": "0.01"}]
    _validate_effective_dimensions(conn, dimensions, prototype)
    cents, remainder = divmod(int(total * 100), periods)
    schedule_id = str(uuid.uuid4())
    results = []
    conn.execute("SAVEPOINT expense_schedule")
    try:
        for offset, due in enumerate(dates):
            amount = format(Decimal(cents + (1 if offset < remainder else 0)) / 100, ".2f")
            lines = [{"account_id": expense_id, "debit": amount, "credit": "0.00"},
                     {"account_id": balance_id, "debit": "0.00", "credit": amount}]
            template_id = str(uuid.uuid4())
            naming = get_next_name(conn, "recurring_journal_template", company_id=company_id)
            values = {
                "id": template_id, "naming_series": naming, "company_id": company_id,
                "name": f"{name.strip()} ({offset + 1}/{periods})", "frequency": "monthly",
                "start_date": due.isoformat(), "end_date": due.isoformat(),
                "next_run_date": due.isoformat(), "entry_type": "journal",
                "lines": json.dumps(lines), "auto_submit": 0, "status": "active",
                "remark": f"{kind} schedule {schedule_id}; {getattr(args, 'remark', None) or name.strip()}",
                "dimensions_json": dimensions_json_text(dimensions),
            }
            sql, columns = insert_row("recurring_journal_template", {key: P() for key in values})
            conn.execute(sql, tuple(values[key] for key in columns))
            audit(conn, "erpclaw-journals", "add-expense-schedule",
                  "recurring_journal_template", template_id,
                  new_values={"schedule_id": schedule_id, "kind": kind,
                              "amount": amount, "due_date": due.isoformat()})
            results.append({"template_id": template_id, "due_date": due.isoformat(),
                            "amount": amount})
        conn.execute("RELEASE SAVEPOINT expense_schedule")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT expense_schedule")
        conn.execute("RELEASE SAVEPOINT expense_schedule")
        raise
    conn.commit()
    ok({"schedule_id": schedule_id, "schedule_kind": kind, "periods": periods,
        "total": format(total, ".2f"), "templates": results, "auto_submit": False,
        "next_step": "process-recurring generates due drafts; review and submit each journal"})


# ---------------------------------------------------------------------------
# 11. add-recurring-template
# ---------------------------------------------------------------------------

def add_recurring_template(conn, args):
    """Create a recurring journal template."""
    company_id = args.company_id
    if not company_id:
        err("--company-id is required")
    template_name = args.template_name
    if not template_name:
        err("--template-name is required")
    frequency = args.frequency or "monthly"
    if frequency not in VALID_FREQUENCIES:
        err(f"Invalid frequency '{frequency}'. Valid: {VALID_FREQUENCIES}")
    start_date = args.start_date
    if not start_date:
        err("--start-date is required")
    end_date = args.end_date  # optional

    entry_type = args.entry_type or "journal"
    if entry_type not in VALID_ENTRY_TYPES:
        err(f"Invalid entry type '{entry_type}'. Valid: {VALID_ENTRY_TYPES}")

    auto_submit = 1 if args.auto_submit else 0

    # Validate company
    q = Q.from_(_t_company).select(_t_company.id).where(_t_company.id == P())
    company = conn.execute(q.get_sql(), (company_id,)).fetchone()
    if not company:
        err(f"Company {company_id} not found")

    # Parse and validate lines
    lines_json = args.lines
    if not lines_json:
        err("--lines is required (JSON array)")
    try:
        lines = json.loads(lines_json) if isinstance(lines_json, str) else lines_json
    except json.JSONDecodeError:
        err("Invalid JSON format in --lines")

    try:
        _validate_lines(lines)
    except ValueError as e:
        err(str(e))

    # Validate accounts exist
    q_acct = Q.from_(_t_account).select(_t_account.id).where(_t_account.id == P())
    for i, line in enumerate(lines):
        acct = conn.execute(q_acct.get_sql(), (line["account_id"],)).fetchone()
        if not acct:
            err(f"Line {i+1}: account {line['account_id']} not found")

    # Accounting dimensions (M6): header input plus per-line objects are
    # parsed and checked against the registry here, before get_next_name
    # consumes a naming-series step. The header object is stored on the
    # template; each line's own object stays inside the lines JSON.
    tmpl_header_input = _parse_header_dimensions(args)
    tmpl_header_obj = tmpl_header_input if tmpl_header_input is not None else {}
    _parse_line_dimensions(lines)
    _validate_effective_dimensions(conn, tmpl_header_obj, lines)

    template_id = str(uuid.uuid4())
    naming = get_next_name(conn, "recurring_journal_template", company_id=company_id)

    conn.execute(
        """INSERT INTO recurring_journal_template
           (id, naming_series, company_id, name, frequency, start_date, end_date,
            next_run_date, entry_type, lines, auto_submit, remark, status,
            dimensions_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
        (template_id, naming, company_id, template_name, frequency,
         start_date, end_date, start_date, entry_type,
         json.dumps(lines) if isinstance(lines, list) else lines_json,
         auto_submit, args.remark,
         dimensions_json_text(tmpl_header_obj)),
    )

    audit(conn, "erpclaw-journals", "add-recurring-template",
          "recurring_journal_template", template_id,
          new_values={"name": template_name, "frequency": frequency})
    conn.commit()

    ok({"status": "created", "template_id": template_id,
        "naming_series": naming, "next_run_date": start_date})


# ---------------------------------------------------------------------------
# 12. update-recurring-template
# ---------------------------------------------------------------------------

def update_recurring_template(conn, args):
    """Update a recurring journal template. Only active/paused templates."""
    template_id = args.template_id
    if not template_id:
        err("--template-id is required")

    q = Q.from_(_t_rjt).select(_t_rjt.star).where(_t_rjt.id == P())
    row = conn.execute(q.get_sql(), (template_id,)).fetchone()
    if not row:
        err(f"Recurring template {template_id} not found")
    tmpl = row_to_dict(row)

    if tmpl["status"] == "completed":
        err("Cannot update a completed template")

    updated_fields = []

    # Accounting dimensions (M6): parse the header input and --lines here,
    # before any UPDATE below, so a refusal writes nothing. Validation runs
    # when the caller supplied header input or --lines; otherwise the stored
    # header and the template's stored lines are checked.
    rec_header_input = _parse_header_dimensions(args)
    rec_lines = None
    if args.lines:
        try:
            rec_lines = (json.loads(args.lines)
                         if isinstance(args.lines, str) else args.lines)
        except json.JSONDecodeError:
            err("Invalid JSON format in --lines")
        try:
            _validate_lines(rec_lines)
        except ValueError as e:
            err(str(e))
        q_acct_rec = Q.from_(_t_account).select(_t_account.id).where(_t_account.id == P())
        for i, line in enumerate(rec_lines):
            acct = conn.execute(q_acct_rec.get_sql(), (line["account_id"],)).fetchone()
            if not acct:
                err(f"Line {i+1}: account {line['account_id']} not found")
        _parse_line_dimensions(rec_lines)
    if rec_header_input is not None or args.lines:
        if rec_header_input is not None:
            rec_validation_header = rec_header_input
        else:
            rec_validation_header = _loads_dims(tmpl.get("dimensions_json"))
        if rec_lines is not None:
            rec_validation_lines = rec_lines
        else:
            try:
                rec_validation_lines = json.loads(tmpl["lines"])
            except (ValueError, TypeError):
                err("Invalid JSON format in template lines")
        _validate_effective_dimensions(
            conn, rec_validation_header, rec_validation_lines)

    if args.template_name:
        conn.execute("UPDATE recurring_journal_template SET name = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.template_name, template_id))
        updated_fields.append("name")

    if args.frequency:
        if args.frequency not in VALID_FREQUENCIES:
            err(f"Invalid frequency '{args.frequency}'. Valid: {VALID_FREQUENCIES}")
        conn.execute("UPDATE recurring_journal_template SET frequency = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.frequency, template_id))
        updated_fields.append("frequency")

    if args.end_date:
        conn.execute("UPDATE recurring_journal_template SET end_date = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.end_date, template_id))
        updated_fields.append("end_date")

    if args.entry_type:
        if args.entry_type not in VALID_ENTRY_TYPES:
            err(f"Invalid entry type '{args.entry_type}'. Valid: {VALID_ENTRY_TYPES}")
        conn.execute("UPDATE recurring_journal_template SET entry_type = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.entry_type, template_id))
        updated_fields.append("entry_type")

    if args.remark is not None:
        conn.execute("UPDATE recurring_journal_template SET remark = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.remark, template_id))
        updated_fields.append("remark")

    if args.auto_submit is not None:
        val = 1 if args.auto_submit else 0
        conn.execute("UPDATE recurring_journal_template SET auto_submit = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (val, template_id))
        updated_fields.append("auto_submit")

    if rec_header_input is not None:
        conn.execute("UPDATE recurring_journal_template SET dimensions_json = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (dimensions_json_text(rec_header_input), template_id))
        updated_fields.append("dimensions")

    if rec_lines is not None:
        lines = rec_lines
        conn.execute("UPDATE recurring_journal_template SET lines = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (json.dumps(lines) if isinstance(lines, list) else args.lines, template_id))
        updated_fields.append("lines")

    if args.template_status:
        if args.template_status not in ("active", "paused"):
            err(f"Can only set status to 'active' or 'paused', got '{args.template_status}'")
        conn.execute("UPDATE recurring_journal_template SET status = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                     (args.template_status, template_id))
        updated_fields.append("status")

    if not updated_fields:
        err("No fields to update")

    audit(conn, "erpclaw-journals", "update-recurring-template",
          "recurring_journal_template", template_id,
          new_values={"updated_fields": updated_fields})
    conn.commit()

    ok({"status": "updated", "template_id": template_id,
        "updated_fields": updated_fields})


# ---------------------------------------------------------------------------
# 13. list-recurring-templates
# ---------------------------------------------------------------------------

def list_recurring_templates(conn, args):
    """List recurring journal templates for a company."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    rjt = Table("recurring_journal_template")
    params = [company_id]

    base = Q.from_(rjt).where(rjt.company_id == P())

    if args.template_status:
        base = base.where(rjt.status == P())
        params.append(args.template_status)

    limit, offset = _parse_paging(args)

    q_count = base.select(fn.Count("*"))
    count_row = conn.execute(q_count.get_sql(), params).fetchone()
    total_count = count_row[0]

    list_params = params + [limit, offset]
    q_list = (base.select(
                  rjt.id, rjt.naming_series, rjt.name, rjt.frequency,
                  rjt.start_date, rjt.end_date, rjt.next_run_date,
                  rjt.last_generated_date, rjt.entry_type, rjt.auto_submit,
                  rjt.remark, rjt.status)
              .orderby(rjt.next_run_date, order=Order.asc)
              .limit(P()).offset(P()))
    rows = conn.execute(q_list.get_sql(), list_params).fetchall()

    templates = [row_to_dict(r) for r in rows]
    ok({"templates": templates, "total_count": total_count,
        "limit": limit, "offset": offset,
        "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 14. get-recurring-template
# ---------------------------------------------------------------------------

def get_recurring_template(conn, args):
    """Get a recurring template with full detail including lines."""
    template_id = args.template_id
    if not template_id:
        err("--template-id is required")

    q = Q.from_(_t_rjt).select(_t_rjt.star).where(_t_rjt.id == P())
    row = conn.execute(q.get_sql(), (template_id,)).fetchone()
    if not row:
        err(f"Recurring template {template_id} not found")

    tmpl = row_to_dict(row)
    # Parse lines JSON for display
    try:
        tmpl["lines"] = json.loads(tmpl["lines"])
    except (json.JSONDecodeError, TypeError):
        pass

    ok(tmpl)


# ---------------------------------------------------------------------------
# 15. process-recurring
# ---------------------------------------------------------------------------

def process_recurring(conn, args):
    """Generate journal entries from all due recurring templates.

    Idempotent: only generates JEs where next_run_date <= as_of_date.
    After generating, advances next_run_date by one frequency period.
    If end_date is reached, marks template as 'completed'.

    S1.3 (Wave F): orchestrated by the crash-safe billing_run registry.
    Each due template is a billing_run_target processed in its OWN
    transaction — JE insert + GL + next_run_date advance + target status
    commit atomically, so a crash mid-run resumes (`--resume-run-id` /
    `resume-billing-run`) with ZERO duplicate journal entries, and one
    bad template (e.g. garbage lines JSON) no longer aborts the run.
    """
    company_id = args.company_id
    if not company_id:
        err("--company-id is required")

    from erpclaw_lib import billing_run as billing_run_lib

    resume_run_id = getattr(args, "resume_run_id", None)
    if resume_run_id:
        run = billing_run_lib.load_run(conn, resume_run_id)
        if run is None:
            err(f"Billing run not found: {resume_run_id}")
        if run["run_type"] != "recurring_journals":
            err(f"Billing run {resume_run_id} has run_type "
                 f"'{run['run_type']}', expected 'recurring_journals'")
        if run["status"] == "completed":
            err(f"Billing run {resume_run_id} is completed; nothing to resume")
        run_id = resume_run_id
        as_of_date_str = run["as_of_date"]
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE billing_run SET status = 'running', finished_at = NULL, "
            "updated_at = ? WHERE id = ?", (ts, run_id))
        conn.commit()
        targets = billing_run_lib.list_targets(
            conn, run_id, statuses=billing_run_lib.RESUMABLE_TARGET_STATUSES)
    else:
        as_of_date_str = args.as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Find all due templates
        q_due = (Q.from_(_t_rjt).select(_t_rjt.id)
                 .where(_t_rjt.company_id == P())
                 .where(_t_rjt.status == "active")
                 .where(_t_rjt.next_run_date <= P())
                 .orderby(_t_rjt.next_run_date, order=Order.asc))
        due_templates = conn.execute(
            q_due.get_sql(), (company_id, as_of_date_str)).fetchall()
        if not due_templates:
            # Same shape as the normal path (QA S1.3 round 1 advisory): a
            # consumer reading run_status after a no-op must not KeyError.
            # No billing_run row is created for zero targets.
            ok({"generated": 0, "results": [], "errors": [], "skipped": 0,
                 "billing_run_id": None, "run_status": "no_work"})
        run_id = billing_run_lib.start(
            conn, "recurring_journals", as_of_date_str,
            [("recurring_journal_template", t["id"]) for t in due_templates],
            company_id=company_id)
        targets = billing_run_lib.list_targets(conn, run_id,
                                               statuses=("pending",))

    results = []
    errors = []
    skipped = 0

    def _callback(cb_conn, target):
        return _process_one_recurring_template(
            cb_conn, target["target_id"], company_id, as_of_date_str)

    for target in targets:
        outcome = billing_run_lib.process_target(conn, run_id, target, _callback)
        if outcome["status"] == "done":
            results.append(outcome["result"]["entry"])
        elif outcome["status"] == "failed":
            errors.append({"template_id": target["target_id"],
                           "error": outcome["error"]})
        elif outcome["status"] == "skipped":
            skipped += 1

    summary = billing_run_lib.finalize(conn, run_id)
    audit(conn, "erpclaw-journals", "process-recurring",
          "recurring_journal_template", company_id,
          new_values={"generated": len(results),
                      "billing_run_id": run_id})
    conn.commit()

    ok({"generated": len(results), "results": results,
         "errors": errors, "skipped": skipped,
         "billing_run_id": run_id, "run_status": summary["status"]})


def _process_one_recurring_template(conn, template_id, company_id, as_of_date_str):
    """Generate ONE template's JE inside the caller's per-target transaction.

    JE + lines + GL (insert_gl_entries, never commits) + the CAS-guarded
    next_run_date advance commit atomically with the target's 'done'
    status. Auto-submit failure keeps its legacy semantic: the JE stays a
    draft and the target still succeeds. Raises on data errors; raises
    billing_run.SkipTarget when the template is no longer due.
    """
    from erpclaw_lib import billing_run as billing_run_lib

    row = conn.execute(
        "SELECT * FROM recurring_journal_template WHERE id = ?",
        (template_id,)).fetchone()
    if not row:
        raise ValueError(f"Template not found: {template_id}")
    tmpl = row_to_dict(row)

    # In-transaction eligibility re-read (resume / concurrent-run safety)
    if tmpl["status"] != "active":
        raise billing_run_lib.SkipTarget(
            f"Template {template_id} is no longer active "
            f"(status '{tmpl['status']}')")
    if tmpl["next_run_date"] > as_of_date_str:
        raise billing_run_lib.SkipTarget(
            f"Template {template_id} is no longer due "
            f"(next_run_date {tmpl['next_run_date']} > as-of {as_of_date_str})")

    lines = json.loads(tmpl["lines"])
    posting_date = tmpl["next_run_date"]

    # Create the journal entry
    je_id = str(uuid.uuid4())
    naming = get_next_name(conn, "journal_entry", company_id=company_id)

    total_debit = sum(to_decimal(l.get("debit", "0")) for l in lines)
    total_credit = sum(to_decimal(l.get("credit", "0")) for l in lines)
    total_debit = round_currency(total_debit)
    total_credit = round_currency(total_credit)

    remark = tmpl.get("remark") or f"Auto-generated from {tmpl['naming_series'] or tmpl['name']}"

    # Accounting dimensions (M6): the template header rides onto the
    # generated entry's header; each template line's own object rides onto
    # the generated line via _insert_lines.
    generated_header = _loads_dims(tmpl.get("dimensions_json"))
    conn.execute(
        """INSERT INTO journal_entry
           (id, naming_series, posting_date, entry_type, total_debit, total_credit,
            remark, status, company_id, dimensions_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (je_id, naming, posting_date, tmpl["entry_type"],
         str(total_debit), str(total_credit), remark, company_id,
         dimensions_json_text(generated_header)),
    )
    _insert_lines(conn, je_id, lines)

    je_status = "draft"

    # Auto-submit if configured
    if tmpl["auto_submit"]:
        try:
            is_opening = tmpl["entry_type"] in ("opening",)
            gl_entries = []
            for l in lines:
                line_dims = l.get("dimensions") or {}
                if not isinstance(line_dims, dict):
                    line_dims = {}
                effective = dict(generated_header)
                effective.update(line_dims)
                # Line wins: a line's own cost center replaces a
                # header-carried cost_center tag in that leg's dimensions
                # before posting, so step 13 sees one cost center.
                if (l.get("cost_center_id") and "cost_center" not in line_dims
                        and effective.get("cost_center")
                        and effective["cost_center"] != l["cost_center_id"]):
                    effective["cost_center"] = l["cost_center_id"]
                gl_entry = {
                    "account_id": l["account_id"],
                    "debit": l.get("debit", "0"),
                    "credit": l.get("credit", "0"),
                    "party_type": l.get("party_type"),
                    "party_id": l.get("party_id"),
                    "cost_center_id": l.get("cost_center_id"),
                }
                if effective:
                    gl_entry["dimensions"] = effective
                gl_entries.append(gl_entry)

            validate_gl_entries(
                conn, gl_entries, company_id, posting_date,
                is_opening=is_opening, voucher_type="journal_entry",
            )
            insert_gl_entries(
                conn, gl_entries,
                voucher_type="journal_entry", voucher_id=je_id,
                posting_date=posting_date, company_id=company_id,
                remarks=remark, is_opening=is_opening,
            )
            conn.execute(
                "UPDATE journal_entry SET status = 'submitted', updated_at = CAST(CURRENT_TIMESTAMP AS TEXT) WHERE id = ?",
                (je_id,),
            )
            je_status = "submitted"
        except (ValueError, Exception) as e:
            sys.stderr.write(f"[erpclaw-journals] Auto-submit failed for {naming}: {e}\n")
            # JE remains as draft (legacy semantic — the target still succeeds)

    # Advance next_run_date — CAS-guarded on the value we read so a
    # concurrent advance rolls THIS template back instead of double-posting
    current_next = date.fromisoformat(tmpl["next_run_date"])
    new_next = _advance_date(current_next, tmpl["frequency"])
    new_next_str = new_next.isoformat()

    # Check if end_date is reached
    new_status = "active"
    if tmpl["end_date"] and new_next_str > tmpl["end_date"]:
        new_status = "completed"

    cur = conn.execute(
        """UPDATE recurring_journal_template
           SET next_run_date = ?, last_generated_date = ?,
               status = ?, updated_at = CAST(CURRENT_TIMESTAMP AS TEXT)
           WHERE id = ? AND next_run_date = ?""",
        (new_next_str, posting_date, new_status, template_id,
         tmpl["next_run_date"]),
    )
    if cur.rowcount != 1:
        raise ValueError(
            f"Template {template_id} advanced concurrently "
            f"(next_run_date moved past {tmpl['next_run_date']}); rolled "
            f"back without posting")

    return {
        "voucher_id": je_id,
        "entry": {
            "template_id": template_id,
            "template_name": tmpl["name"],
            "journal_entry_id": je_id,
            "naming_series": naming,
            "posting_date": posting_date,
            "je_status": je_status,
            "next_run_date": new_next_str if new_status == "active" else None,
            "template_status": new_status,
        },
    }


# ---------------------------------------------------------------------------
# 16. delete-recurring-template
# ---------------------------------------------------------------------------

def delete_recurring_template(conn, args):
    """Delete a recurring template (soft delete: marks as completed)."""
    template_id = args.template_id
    if not template_id:
        err("--template-id is required")

    q = Q.from_(_t_rjt).select(_t_rjt.star).where(_t_rjt.id == P())
    row = conn.execute(q.get_sql(), (template_id,)).fetchone()
    if not row:
        err(f"Recurring template {template_id} not found")

    q_del = Q.from_(_t_rjt).delete().where(_t_rjt.id == P())
    conn.execute(q_del.get_sql(), (template_id,))

    audit(conn, "erpclaw-journals", "delete-recurring-template",
          "recurring_journal_template", template_id)
    conn.commit()

    ok({"status": "deleted", "deleted": True})


# ---------------------------------------------------------------------------
# Month-end close v1
#
# One truthful server-side preview plus one bounded execution over the
# existing recurring-template and journal lifecycles. Neither action closes
# a fiscal year, locks a period, posts straight to the ledger, or reports
# success while draft close entries remain.
# ---------------------------------------------------------------------------

def _parse_month_end_date(args):
    """Return the month-end date (YYYY-MM-DD) or refuse."""
    raw_month = getattr(args, "month_end_date", None)
    raw_asof = getattr(args, "as_of_date", None)
    if raw_month and raw_asof and raw_month != raw_asof:
        err("--as-of-date and --month-end-date differ; pass one month-end date")
    raw = raw_month or raw_asof
    flag = "--month-end-date" if raw_month else "--as-of-date"
    if not raw:
        err("--as-of-date is required")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
        if parsed.strftime("%Y-%m-%d") != raw:
            raise ValueError()
    except (TypeError, ValueError):
        err("Invalid %s '%s': expected YYYY-MM-DD" % (flag, raw))
    return parsed.strftime("%Y-%m-%d")


def _require_close_company(conn, args):
    """Return the company id, refusing when it is missing or unknown."""
    company_id = getattr(args, "company_id", None)
    if not company_id:
        err("--company-id is required")
    q = Q.from_(_t_company).select(_t_company.id).where(_t_company.id == P())
    found = conn.execute(q.get_sql(), (company_id,)).fetchone()
    if not found:
        err("Company not found: %s" % company_id)
    return company_id


def _close_fiscal_year(conn, company_id, month_end):
    """First fiscal year of this company covering month_end, or None."""
    q = (Q.from_(_t_fy)
         .select(_t_fy.id, _t_fy.name, _t_fy.start_date, _t_fy.end_date,
                 _t_fy.is_closed)
         .where(_t_fy.company_id == P())
         .where(_t_fy.start_date <= P())
         .where(_t_fy.end_date >= P())
         .orderby(_t_fy.id, order=Order.asc))
    rows = conn.execute(q.get_sql(), (company_id, month_end, month_end)).fetchall()
    return rows[0] if rows else None


def _close_draft_entries(conn, company_id, month_end):
    """Draft journal entries of this company posted on or before month_end."""
    q = (Q.from_(_t_je)
         .select(_t_je.id, _t_je.posting_date, _t_je.total_debit,
                 _t_je.total_credit)
         .where(_t_je.company_id == P())
         .where(_t_je.status == "draft")
         .where(_t_je.posting_date <= P())
         .orderby(_t_je.posting_date, order=Order.asc)
         .orderby(_t_je.id, order=Order.asc))
    return conn.execute(q.get_sql(), (company_id, month_end)).fetchall()


def _close_due_templates(conn, company_id, month_end):
    """Active recurring templates of this company due on or before month_end."""
    q = (Q.from_(_t_rjt)
         .select(_t_rjt.id, _t_rjt.name, _t_rjt.next_run_date)
         .where(_t_rjt.company_id == P())
         .where(_t_rjt.status == "active")
         .where(_t_rjt.next_run_date <= P())
         .orderby(_t_rjt.next_run_date, order=Order.asc)
         .orderby(_t_rjt.id, order=Order.asc))
    return conn.execute(q.get_sql(), (company_id, month_end)).fetchall()


def _build_month_end_preview(conn, company_id, month_end):
    """Shared read-only preview payload; performs no writes."""
    drafts = _close_draft_entries(conn, company_id, month_end)
    due = _close_due_templates(conn, company_id, month_end)
    fy = _close_fiscal_year(conn, company_id, month_end)

    draft_entries = [{
        "journal_entry_id": row["id"],
        "posting_date": row["posting_date"],
        "total_debit": row["total_debit"],
        "total_credit": row["total_credit"],
    } for row in drafts]
    due_templates = [{
        "template_id": row["id"],
        "name": row["name"],
        "next_run_date": row["next_run_date"],
    } for row in due]

    if fy is None:
        fy_state = "missing"
        fy_info = None
    else:
        fy_state = "closed" if fy["is_closed"] else "open"
        fy_info = {
            "fiscal_year_id": fy["id"],
            "name": fy["name"],
            "start_date": fy["start_date"],
            "end_date": fy["end_date"],
            "is_closed": bool(fy["is_closed"]),
        }

    blockers = []
    if draft_entries:
        blockers.append({
            "code": "draft_journal_entries",
            "count": len(draft_entries),
            "journal_entry_ids": [entry["journal_entry_id"]
                                  for entry in draft_entries],
        })
    if due_templates:
        blockers.append({
            "code": "due_recurring_templates",
            "count": len(due_templates),
            "template_ids": [tmpl["template_id"] for tmpl in due_templates],
        })
    if fy_state == "missing":
        blockers.append({"code": "fiscal_year_missing"})
    elif fy_state == "closed":
        blockers.append({
            "code": "fiscal_year_closed",
            "fiscal_year_id": fy_info["fiscal_year_id"],
            "fiscal_year_name": fy_info["name"],
        })

    return {
        "company_id": company_id,
        "month_end_date": month_end,
        "can_close": not blockers,
        "draft_journal_count": len(draft_entries),
        "draft_journal_entries": draft_entries,
        "due_template_count": len(due_templates),
        "due_recurring_templates": due_templates,
        "fiscal_year_state": fy_state,
        "fiscal_year": fy_info,
        "blockers": blockers,
    }


def journal_month_end_close_preview(conn, args):
    """Read-only month-end close preview for one company through one date.

    Returns the draft journal entries and due active recurring templates
    with the exact stored ids and amounts, plus the missing-or-closed
    fiscal-year condition. Every row is scoped to the company. Writes
    nothing: no entries, no ledger posts, no audit row.
    """
    company_id = _require_close_company(conn, args)
    month_end = _parse_month_end_date(args)
    ok(_build_month_end_preview(conn, company_id, month_end))


def _parse_close_template_ids(args):
    """Explicit template id list in the module's JSON-argument style."""
    raw = getattr(args, "template_ids", None)
    if not raw:
        err("--template-ids is required")
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        err("--template-ids must be valid JSON")
    if not isinstance(ids, list) or not ids:
        err("--template-ids must be a non-empty JSON array")
    for tid in ids:
        if not isinstance(tid, str) or not tid:
            err("--template-ids must be a non-empty JSON array of template ID strings")
    seen = set()
    for tid in ids:
        if tid in seen:
            err("Duplicate template ID in --template-ids: %s" % tid)
        seen.add(tid)
    return ids


def journal_run_month_end_close(conn, args):
    """Bounded month-end close run over explicitly named recurring templates.

    Refuses a template outside the company, an inactive template, a
    template not due by the date, a closed or missing fiscal year, a
    duplicate id, or any existing draft journal entry through the date,
    writing nothing in every refusal case. Otherwise processes only the
    named templates through the existing recurring-journal generator
    inside one transaction, then returns the created journal ids with
    their truthful lifecycle state plus a fresh preview. Reports
    "incomplete" while any created entry remains a draft. Never closes a
    fiscal year, locks a period, or posts straight to the ledger.
    """
    company_id = _require_close_company(conn, args)
    month_end = _parse_month_end_date(args)
    template_ids = _parse_close_template_ids(args)

    fy = _close_fiscal_year(conn, company_id, month_end)
    if fy is None:
        err("No fiscal year covers %s for company %s" % (month_end, company_id))
    if fy["is_closed"]:
        err("Fiscal year '%s' is closed" % fy["name"])

    drafts = _close_draft_entries(conn, company_id, month_end)
    if drafts:
        err("Draft journal entries remain through %s: %s" % (
            month_end, ", ".join(row["id"] for row in drafts)))

    q_tmpl = Q.from_(_t_rjt).select(_t_rjt.star).where(_t_rjt.id == P())
    for tid in template_ids:
        row = conn.execute(q_tmpl.get_sql(), (tid,)).fetchone()
        if not row:
            err("Recurring template %s not found" % tid)
        tmpl = row_to_dict(row)
        if tmpl["company_id"] != company_id:
            err("Recurring template %s belongs to another company" % tid)
        if tmpl["status"] != "active":
            err("Recurring template %s is not active (status '%s')"
                % (tid, tmpl["status"]))
        if tmpl["next_run_date"] > month_end:
            err("Recurring template %s is not due by %s (next_run_date %s)"
                % (tid, month_end, tmpl["next_run_date"]))

    created = []
    try:
        for tid in template_ids:
            outcome = _process_one_recurring_template(
                conn, tid, company_id, month_end)
            entry = outcome["entry"]
            q_je = (Q.from_(_t_je)
                    .select(_t_je.id, _t_je.posting_date, _t_je.total_debit,
                            _t_je.total_credit, _t_je.status)
                    .where(_t_je.id == P()))
            stored = conn.execute(
                q_je.get_sql(), (entry["journal_entry_id"],)).fetchone()
            created.append({
                "journal_entry_id": stored["id"],
                "template_id": tid,
                "je_status": stored["status"],
                "posting_date": stored["posting_date"],
                "total_debit": stored["total_debit"],
                "total_credit": stored["total_credit"],
            })
    except Exception as exc:
        conn.rollback()
        err(unexpected_error_message(exc))

    audit(conn, "erpclaw-journals", "journal-run-month-end-close",
          "recurring_journal_template", company_id,
          new_values={"processed": len(created),
                      "created_journal_ids": [entry["journal_entry_id"]
                                              for entry in created]})
    conn.commit()

    preview = _build_month_end_preview(conn, company_id, month_end)
    complete = (all(entry["je_status"] == "submitted" for entry in created)
                and preview["can_close"])

    ok({
        "company_id": company_id,
        "month_end_date": month_end,
        "processed": len(created),
        "created_journal_ids": [entry["journal_entry_id"]
                                for entry in created],
        "created_journals": created,
        "state": "complete" if complete else "incomplete",
        "preview": preview,
    })


# ---------------------------------------------------------------------------
# 17. status
# ---------------------------------------------------------------------------

def status(conn, args):
    """Show journal entry counts by status."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    q_je = (Q.from_(_t_je)
            .select(_t_je.status, fn.Count("*").as_("cnt"))
            .where(_t_je.company_id == P())
            .groupby(_t_je.status))
    rows = conn.execute(q_je.get_sql(), (company_id,)).fetchall()

    counts = {"total": 0, "draft": 0, "submitted": 0, "cancelled": 0, "amended": 0}
    for row in rows:
        counts[row["status"]] = row["cnt"]
        counts["total"] += row["cnt"]

    # Recurring template counts
    q_rjt = (Q.from_(_t_rjt)
             .select(_t_rjt.status, fn.Count("*").as_("cnt"))
             .where(_t_rjt.company_id == P())
             .groupby(_t_rjt.status))
    tmpl_rows = conn.execute(q_rjt.get_sql(), (company_id,)).fetchall()
    recurring = {"active": 0, "paused": 0, "completed": 0}
    for r in tmpl_rows:
        recurring[r["status"]] = r["cnt"]
    counts["recurring_templates"] = recurring

    ok(counts)


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

ACTIONS = {
    "add-journal-entry": add_journal_entry,
    "create-expense-allocation": create_expense_allocation,
    "add-interfund-transfer": add_interfund_transfer,
    "update-journal-entry": update_journal_entry,
    "get-journal-entry": get_journal_entry,
    "list-journal-entries": list_journal_entries,
    "submit-journal-entry": submit_journal_entry,
    "cancel-journal-entry": cancel_journal_entry,
    "amend-journal-entry": amend_journal_entry,
    "delete-journal-entry": delete_journal_entry,
    "duplicate-journal-entry": duplicate_journal_entry,
    "create-intercompany-je": create_intercompany_je,
    "add-recurring-template": add_recurring_template,
    "add-expense-schedule": add_expense_schedule,
    "update-recurring-template": update_recurring_template,
    "list-recurring-templates": list_recurring_templates,
    "get-recurring-template": get_recurring_template,
    "process-recurring": process_recurring,
    "delete-recurring-template": delete_recurring_template,
    "journal-month-end-close-preview": journal_month_end_close_preview,
    "journal-run-month-end-close": journal_run_month_end_close,
    "status": status,
}


def main():
    parser = SafeArgumentParser(description="ERPClaw Journals Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Journal entry fields
    parser.add_argument("--journal-entry-id")
    parser.add_argument("--company-id")
    parser.add_argument("--company", dest="company_name", default=None)  # NL: company by name
    parser.add_argument("--posting-date")
    parser.add_argument("--entry-type")
    parser.add_argument("--remark")
    parser.add_argument("--lines")
    parser.add_argument("--allocations")
    parser.add_argument("--source-account-id")
    parser.add_argument("--source-cost-center-id")
    parser.add_argument("--amended-from")
    # S3 CWIP hook (AVA-43): capitalise this JE's CWIP debit leg to an asset
    parser.add_argument("--cwip-asset-id")

    # Accounting dimensions (M6): header tags for every draft action below
    parser.add_argument("--dimensions", default=None)
    parser.add_argument("--dimension-key", dest="dimension_key",
                        action="append", default=None)
    parser.add_argument("--dimension-value", dest="dimension_value",
                        action="append", default=None)

    # Intercompany fields
    parser.add_argument("--source-company-id")
    parser.add_argument("--target-company-id")
    parser.add_argument("--amount")
    parser.add_argument("--description")
    parser.add_argument("--fund-dimension", default="fund")
    parser.add_argument("--from-fund")
    parser.add_argument("--to-fund")
    parser.add_argument("--source-cash-account-id")
    parser.add_argument("--target-cash-account-id")
    parser.add_argument("--due-from-account-id")
    parser.add_argument("--due-to-account-id")

    # Recurring template fields
    parser.add_argument("--template-id")
    parser.add_argument("--template-name")
    parser.add_argument("--schedule-kind")
    parser.add_argument("--periods")
    parser.add_argument("--expense-account-id")
    parser.add_argument("--balance-account-id")
    parser.add_argument("--frequency")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--auto-submit", action="store_true", default=None)
    parser.add_argument("--as-of-date")
    parser.add_argument("--month-end-date")  # floor-o040: alias for the close date
    parser.add_argument("--template-ids")  # floor-o040: JSON array of template ids
    parser.add_argument("--resume-run-id")  # S1.3: resume a crashed billing_run
    parser.add_argument("--template-status")

    # List filters
    parser.add_argument("--status", dest="je_status")
    parser.add_argument("--account-id")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")

    raw = sys.argv[1:]
    try:
        parse_argv, _auth_id = authority_gate.split_authorization_id(raw)
    except ValueError:
        err(INPUT_INVALID)
    args, unknown = parser.parse_known_args(parse_argv)
    check_unknown_args(parser, unknown)
    check_input_lengths(args)
    action_fn = ACTIONS[args.action]

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
        authority_gate.run(conn, args.action, raw, lambda handle: action_fn(handle, args), option_strings=[s for a in parser._actions for s in a.option_strings], repeatable_options=[s for a in parser._actions if isinstance(a, argparse._AppendAction) for s in a.option_strings])
    except authority_gate.AuthorityRefusal as refusal:
        conn.rollback()
        err(refusal.args[0], suggestion=authority_gate.SUGGESTIONS.get(refusal.args[0]))
    except ValueError as exc:
        if exc.args == (INPUT_INVALID,):
            conn.rollback()
            err(INPUT_INVALID)
        conn.rollback()
        sys.stderr.write(f"[erpclaw-journals] {exc}\n")
        err(unexpected_error_message(exc))
    except Exception as e:
        conn.rollback()
        sys.stderr.write(f"[erpclaw-journals] {e}\n")
        err(unexpected_error_message(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
