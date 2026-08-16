#!/usr/bin/env python3
"""ERPClaw Payments Skill — db_query.py

Payment entries, allocations, payment ledger, and reconciliation.
Draft→Submit→Cancel lifecycle. Submit posts GL entries via shared lib.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# Add shared lib to path
try:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH
    from erpclaw_lib.decimal_utils import to_decimal, round_currency
    from erpclaw_lib.validation import check_input_lengths
    from erpclaw_lib.gl_posting import (
        validate_gl_entries,
        insert_gl_entries,
        reverse_gl_entries,
        prepare_multicurrency_entries,
    )
    from erpclaw_lib.fx_posting import (
        calculate_exchange_gain_loss,
        post_exchange_gain_loss,
    )
    from erpclaw_lib.payment_clearing import (
        apply_payment_to_document,
        reverse_payment_on_document,
        recalc_unallocated,
        post_party_residual_compensation,
        canonical_voucher_type,
    )
    # Aliased to match erpclaw-reports, where a bare `party_ledger`
    # would be shadowed by that module's own `party-ledger` action.
    from erpclaw_lib import party_ledger as party_ledger_rules
    from erpclaw_lib.naming import get_next_name
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
    from erpclaw_lib.query_helpers import resolve_company_id, get_fiscal_year
    from erpclaw_lib.query import (
        Q, P, Table, Field, fn, Order, DecimalSum, insert_row, update_row, now,
    )
    from erpclaw_lib.vendor.pypika.terms import LiteralValue, ValueWrapper
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw first: clawhub install erpclaw", "suggestion": "clawhub install erpclaw"}))
    sys.exit(1)

REQUIRED_TABLES = ["company", "account"]

VALID_PAYMENT_TYPES = ("receive", "pay", "internal_transfer")
# Standard party types (used only as the error-message hint). Authoritative
# validity now comes from party_type_registry via _party_type_registered (M0
# phase 3b): the hardcoded CHECKs on payment_entry/payment_ledger_entry.party_type
# were dropped so party types are registry-sourced + extensible at runtime.
VALID_PARTY_TYPES = ("customer", "supplier", "employee")


def _party_type_registered(conn, party_type):
    """True if party_type exists in party_type_registry (M0 phase 3b source of truth)."""
    return conn.execute(
        "SELECT 1 FROM party_type_registry WHERE party_type = ? AND is_active = 1", (party_type,)
    ).fetchone() is not None

# ── PyPika table aliases ──
PE = Table("payment_entry")
PA = Table("payment_allocation")
PLE = Table("payment_ledger_entry")
COMPANY = Table("company")
ACCOUNT = Table("account")
GL = Table("gl_entry")
CC = Table("cost_center")
PT = Table("payment_terms")
SI = Table("sales_invoice")
PI = Table("purchase_invoice")
PD = Table("payment_deduction")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_pe_or_err(conn, payment_entry_id: str) -> dict:
    """Fetch a payment entry by ID. Calls err() if not found."""
    q = Q.from_(PE).select(PE.star).where(PE.id == P())
    row = conn.execute(q.get_sql(), (payment_entry_id,)).fetchone()
    if not row:
        err(f"Payment entry {payment_entry_id} not found",
             suggestion="Use 'list payments' to see available payment entries.")
    return row_to_dict(row)


def _get_allocations(conn, payment_entry_id: str) -> list[dict]:
    """Fetch the LIVE allocations for a payment entry.

    Wave G F1: an allocation released by a document cancel carries
    delinked = 1. It stays in the table as the audit trail but is void — it
    clears nothing, consumes no residual, and must not appear in any
    aggregation or in get-payment's allocation list.
    """
    # Stable, dialect-portable insertion-ish order: created_at then id.
    # (rowid is SQLite-only and absent on Postgres.)
    q = (Q.from_(PA).select(PA.star)
         .where(PA.payment_entry_id == P())
         .where(PA.delinked == P())
         .orderby(PA.created_at).orderby(PA.id))
    rows = conn.execute(q.get_sql(), (payment_entry_id, 0)).fetchall()
    return [row_to_dict(r) for r in rows]


def _insert_allocations(conn, payment_entry_id: str, allocations: list[dict]):
    """Insert payment allocation rows and return total allocated."""
    sql, _ = insert_row("payment_allocation", {
        "id": P(), "payment_entry_id": P(), "voucher_type": P(),
        "voucher_id": P(), "allocated_amount": P(),
    })
    total_allocated = Decimal("0")
    for alloc in allocations:
        alloc_id = str(uuid.uuid4())
        amount = round_currency(to_decimal(alloc.get("allocated_amount", "0")))
        total_allocated += amount
        # Canonicalize voucher_type at the write boundary so the gateway's
        # doctype-label form ("Sales Invoice") is stored as the snake_case key
        # the clearing / PLE / INV-22 paths compare against.
        vtype = canonical_voucher_type(alloc["voucher_type"])
        conn.execute(sql, (alloc_id, payment_entry_id,
                           vtype, alloc["voucher_id"], str(amount)))
    return total_allocated


INVOICE_VOUCHER_TYPES = ("sales_invoice", "purchase_invoice")

# WS2 D3: mirrors the payment_deduction.type CHECK enum in init_schema.py.
# Validated here so a bad type errors with clean JSON, never a raw IntegrityError.
# 'write_off' (Wave G F17b, migration 034) is the residual a customer never pays,
# taken AT PAYMENT TIME against real cash. This gate runs BEFORE the CHECK is
# ever reached, so widening only the schema would have left the value unreachable
# through every real entry point — the two move together or not at all.
VALID_DEDUCTION_TYPES = ("tds", "commission", "early_payment_discount",
                         "write_off", "other")


def _get_deductions(conn, payment_entry_id: str) -> list[dict]:
    """Fetch deduction rows for a payment entry (stable created_at, id order)."""
    q = (Q.from_(PD).select(PD.star)
         .where(PD.payment_entry_id == P())
         .orderby(PD.created_at).orderby(PD.id))
    rows = conn.execute(q.get_sql(), (payment_entry_id,)).fetchall()
    return [row_to_dict(r) for r in rows]


def _insert_deductions(conn, payment_entry_id: str, payment_type: str,
                       deductions_arg) -> Decimal:
    """Validate + insert payment_deduction rows (WS2 D3). Returns total deducted.

    Each entry: {"account_id", "amount", "type", "description"?}. Deductions are
    the non-cash slice of paid_amount (discount given, TDS withheld, processor
    commission, write-off): paid_amount = allocations + deductions + unallocated.
    """
    if payment_type == "internal_transfer":
        err("--deductions is not supported for internal_transfer payments")
    try:
        deds = json.loads(deductions_arg) if isinstance(deductions_arg, str) else deductions_arg
    except json.JSONDecodeError:
        err("Invalid JSON format in --deductions")
    if not isinstance(deds, list) or not deds or \
            not all(isinstance(d, dict) for d in deds):
        err("--deductions must be a non-empty JSON array of objects")
    qa = Q.from_(ACCOUNT).select(ACCOUNT.id).where(ACCOUNT.id == P())
    sql, _ = insert_row("payment_deduction", {
        "id": P(), "payment_entry_id": P(), "account_id": P(),
        "amount": P(), "type": P(), "description": P(),
    })
    total = Decimal("0")
    for d in deds:
        acct_id = d.get("account_id")
        if not acct_id or not conn.execute(qa.get_sql(), (acct_id,)).fetchone():
            err(f"Deduction account {acct_id} not found")
        dtype = d.get("type")
        if dtype not in VALID_DEDUCTION_TYPES:
            err(f"Invalid deduction type '{dtype}'. Valid: {VALID_DEDUCTION_TYPES}")
        try:
            amount = round_currency(to_decimal(d.get("amount", "0")))
        except (TypeError, ValueError, InvalidOperation):
            err(f"Invalid deduction amount {d.get('amount')!r} "
                "(pass money as a string, e.g. \"20.00\", never a float)")
        if amount <= 0:
            err("Deduction amounts must be > 0")
        total += amount
        conn.execute(sql, (str(uuid.uuid4()), payment_entry_id, acct_id,
                           str(amount), dtype, d.get("description")))
    return total


def _deduction_shares(allocations, total_deductions) -> dict:
    """Pro-rata split of the deduction total across invoice-type allocations.

    Each non-final share is rounded; the final invoice allocation absorbs the
    residue so the shares sum EXACTLY to total_deductions. Returns
    {allocation_id: Decimal}. Empty when there is nothing to distribute (no
    deductions, or no invoice allocations — an on-account deduction stays at
    party level and never clears a document).
    """
    shares = {}
    if total_deductions <= 0:
        return shares
    inv_allocs = [a for a in allocations
                  if canonical_voucher_type(a["voucher_type"]) in INVOICE_VOUCHER_TYPES]
    if not inv_allocs:
        return shares
    alloc_sum = sum((to_decimal(a["allocated_amount"]) for a in inv_allocs),
                    Decimal("0"))
    if alloc_sum <= 0:
        return shares
    remaining = total_deductions
    for a in inv_allocs[:-1]:
        share = round_currency(
            total_deductions * to_decimal(a["allocated_amount"]) / alloc_sum)
        if share > remaining:
            share = remaining
        shares[a["id"]] = share
        remaining -= share
    shares[inv_allocs[-1]["id"]] = round_currency(remaining)
    return shares


def _default_cost_center(conn, company_id: str):
    """Company default cost center, else the first non-group CC, else None."""
    row = conn.execute(
        Q.from_(COMPANY).select(COMPANY.default_cost_center_id)
        .where(COMPANY.id == P()).get_sql(), (company_id,)).fetchone()
    if row and row["default_cost_center_id"]:
        return row["default_cost_center_id"]
    qc = (Q.from_(CC).select(CC.id)
          .where(CC.company_id == P()).where(CC.is_group == P()))
    cc = conn.execute(qc.get_sql() + " LIMIT 1", (company_id, 0)).fetchone()
    return cc["id"] if cc else None


def _apply_deduction_legs(conn, pe, gl_entries, deductions, total_deductions):
    """Mutate gl_entries for WS2 D3: shrink the cash leg by total_deductions and
    append one same-side leg per deduction row (DR for receive, CR for pay).

    The party control leg keeps the FULL paid_amount, so the invoice-clearing
    credit/debit legs are unchanged and debits still equal credits:
      receive: DR bank (paid − deductions) + DR deduction accts / CR AR paid
      pay:     DR AP paid / CR bank (paid − deductions) + CR deduction accts
    P&L deduction accounts get the default cost center (12-step Step 6). The
    caller runs the FULL validate_gl_entries pass on the mutated list — the
    deduction legs go through the same 12-step gate as every other leg.
    """
    if pe["payment_type"] == "receive":
        cash_acct, side = pe["paid_to_account"], "debit"
    else:  # pay
        cash_acct, side = pe["paid_from_account"], "credit"
    for e in gl_entries:
        if e["account_id"] == cash_acct and to_decimal(e.get(side, "0")) > 0:
            cur = to_decimal(e[side])
            if total_deductions > cur:
                err(f"Deductions total ({total_deductions}) exceeds the "
                    f"cash leg ({cur})")
            e[side] = str(round_currency(cur - total_deductions))
            break
    else:
        err("Cash leg not found for deduction posting")
    default_cc = None
    for d in deductions:
        leg = {"account_id": d["account_id"], "debit": "0", "credit": "0",
               "party_type": None, "party_id": None}
        leg[side] = d["amount"]
        acct = conn.execute(
            Q.from_(ACCOUNT).select(ACCOUNT.root_type)
            .where(ACCOUNT.id == P()).get_sql(), (d["account_id"],)).fetchone()
        if acct and acct["root_type"] in ("income", "expense"):
            if default_cc is None:
                default_cc = _default_cost_center(conn, pe["company_id"]) or ""
            if default_cc:
                leg["cost_center_id"] = default_cc
        gl_entries.append(leg)


def _post_allocation_ple(conn, pe, voucher_type, voucher_id, allocated_amount):
    """Insert a per-allocation PLE row that offsets an invoice's voucher PLE.

    The invoice voucher PLE (+grand_total, posted at invoice submit) plus this
    per-allocation payment PLE (-allocated, against_voucher = the invoice) net to
    zero when the invoice is fully paid (INV-22). RETAINS the separate party-level
    payment PLE (-paid_amount, no against_voucher) posted at submit_payment.

    Uses the SAME receivable/payable account as the party-level payment PLE
    (paid_from for 'receive', paid_to for 'pay'). Subledger, NOT gl_entry — does
    not route through gl_posting / the 12-step GL validation. Caller owns the
    transaction; this does NOT commit.
    """
    voucher_type = canonical_voucher_type(voucher_type)
    if voucher_type not in INVOICE_VOUCHER_TYPES:
        return None
    if not (pe.get("party_type") and pe.get("party_id")):
        return None

    ple_account = pe["paid_from_account"] if pe["payment_type"] == "receive" \
        else pe["paid_to_account"]
    ple_amount = str(round_currency(-to_decimal(allocated_amount)))
    ple_id = str(uuid.uuid4())
    ple_sql, _ = insert_row("payment_ledger_entry", {
        "id": P(), "posting_date": P(), "account_id": P(),
        "party_type": P(), "party_id": P(),
        "voucher_type": P(), "voucher_id": P(),
        "against_voucher_type": P(), "against_voucher_id": P(),
        "amount": P(), "amount_in_account_currency": P(),
        "currency": P(), "remarks": P(),
    })
    conn.execute(ple_sql,
        (ple_id, pe["posting_date"], ple_account,
         pe["party_type"], pe["party_id"],
         "payment_entry", pe["id"],
         voucher_type, voucher_id,
         ple_amount, ple_amount,
         pe["payment_currency"],
         f"Payment {pe['naming_series']} applied to {voucher_type} {voucher_id}"))
    return ple_id


def _clear_invoice_allocation(conn, pe, voucher_type, voucher_id, allocated_amount):
    """Apply ONE allocation to its document: sync outstanding/status + post PLE.

    Single site combining the two halves so all three entry points
    (submit/allocate/reconcile) clear an allocation identically. Caller owns the
    transaction. Raises ValueError on a clearing error (over-application / bad
    status) — the caller translates to its JSON error contract and rolls back.

    Returns True if a document was actually cleared (an invoice synced + PLE
    posted), False for the legitimate no-op path (advance / on-account voucher
    types that never clear a document). Lets callers distinguish "nothing to
    clear because not an invoice" from a real clearing, so submit/allocate/
    reconcile never report a false success.
    """
    # Defensive canonicalization: handle any pre-existing label-form value that
    # was stored before the write-boundary fix (e.g. on the live box) so the
    # INVOICE_VOUCHER_TYPES membership test and the per-allocation PLE both use
    # the canonical snake_case form.
    voucher_type = canonical_voucher_type(voucher_type)
    if voucher_type not in INVOICE_VOUCHER_TYPES:
        return False
    apply_payment_to_document(conn, voucher_type, voucher_id, allocated_amount)
    _post_allocation_ple(conn, pe, voucher_type, voucher_id, allocated_amount)
    return True


def _recalc_unallocated(conn, payment_entry_id: str):
    """Recalculate and update unallocated_amount on a payment entry.

    WS2 D3 invariant: paid_amount = allocations + deductions + unallocated.
    Deductions are non-cash consumption of the payment (discount/TDS/commission)
    and reduce the allocatable remainder exactly like allocations do.

    Delegates to the neutral clearing lib (Wave G F1): a document cancel
    releases its allocations through the SAME residual formula
    (payment_clearing.release_allocations_on_document), and one live copy of
    that arithmetic is the only way the two paths cannot drift. The lib counts
    only LIVE allocations (payment_allocation.delinked = 0) — a released
    allocation returns its cash to the residual.

    Returns the residual the lib wrote (Decimal), or None when the payment is
    absent, so a caller can act on it without re-reading the column it just
    wrote — M60's non-negative guard is the first such caller.
    """
    return recalc_unallocated(conn, payment_entry_id)


def _refuse_negative_residual(conn, payment_entry_id: str, residual):
    """Roll back the current edit and refuse it, because it left paid < consumed.

    Only ``update_payment`` calls this today (M60): lowering ``paid_amount``
    below what the payment has already committed to allocations and deductions
    would write a negative ``unallocated_amount``, which is not a residual at
    all — the identity would still balance arithmetically while describing an
    impossible payment, and that column is what INV-27 reads once the payment is
    submitted.

    THE ROLLBACK IS THE POINT, not defensive habit. The recompute runs after the
    writes (so the guard reads the real post-change detail rows rather than a
    second, drifting copy of the residual formula), which means the refused
    write physically exists at the moment we decide to refuse it. ``err`` raises
    SystemExit, and every other error site in this module relies on the CLI
    process dying before any ``commit()`` — true for a CLI invocation, false for
    any caller holding the connection, which is exactly how the refusal is
    tested. Rolling back makes "a refused update leaves nothing behind" a fact
    instead of a side effect of process teardown. Safe because
    ``update_payment`` has no in-tree caller but the action dispatcher, so there
    is never a caller's work in this transaction to discard.

    Reads the terms BEFORE rolling back: afterwards the detail rows describe the
    pre-edit state, and the message must name the numbers the user asked for.
    """
    pe = _get_pe_or_err(conn, payment_entry_id)
    paid = round_currency(to_decimal(pe["paid_amount"]))
    allocated = round_currency(sum(
        (to_decimal(a["allocated_amount"])
         for a in _get_allocations(conn, payment_entry_id)), Decimal("0")))
    deducted = round_currency(sum(
        (to_decimal(d["amount"])
         for d in _get_deductions(conn, payment_entry_id)), Decimal("0")))
    consumed = round_currency(allocated + deducted)
    conn.rollback()
    err(f"--paid-amount {paid} is below what this payment already consumes: "
        f"allocations {allocated} + deductions {deducted} = {consumed} "
        f"(paid_amount = allocations + deductions + unallocated, and the "
        f"residual cannot be negative; this edit would make it {residual})",
        suggestion=(f"Raise --paid-amount to at least {consumed}, or pass "
                    "--allocations in the same call to reduce what the payment "
                    "consumes."))


def _post_party_residual_compensation(conn, payment_entry_id: str):
    """Append the party-level residual compensation row (M38 / Wave G F2, W16).

    Delegates to the neutral clearing lib for the same reason
    ``_recalc_unallocated`` does: a document cancel re-runs this arithmetic from
    ``payment_clearing.release_allocations_on_document``, and one live copy is
    the only way the two paths cannot drift.

    LIFECYCLE SITES, NOT RECALC SITES (correction C3 — the invocation rule was
    wrong at both ends when it was first written, measured against the live
    call-site map). This fires at exactly four places:

      1. ``submit_payment``, right after the party-level row is written and the
         status flips to 'submitted', in the same transaction. ``submit_payment``
         never calls ``_recalc_unallocated`` — allocations and the residual are
         computed at ADD time — so a recalc-site rule would have missed the most
         common lifecycle entirely (``add-payment --allocations`` then
         ``submit-payment``) and the 400-vs-700 defect would have survived the
         fix on the mainline flow.
      2. ``allocate_payment`` (already submitted-guarded).
      3. ``reconcile_payments``, per affected payment.
      4. F1's release helper, inside the clearing lib.

    It must NEVER fire from ``add_payment`` / ``update_payment`` (both draft
    sites — a draft compensation reads RED against the invariant's RHS) or from
    ``delete_payment`` (which deletes allocations, deductions and the payment but
    no ledger rows, so a draft-written compensation row would be orphaned
    forever). There is no post-submit deduction mutation in this module today:
    ``update_payment`` refuses anything that is not a draft, so the deduction
    tables only change before submit. If that ever changes, the new site joins
    this list.
    """
    return post_party_residual_compensation(conn, payment_entry_id)


def _validate_not_group_account(conn, account_id: str, label: str) -> str:
    """Validate that an account is not a group account.

    If the account is a group (is_group = 1), attempt to find the first
    leaf child account under it.  If a single leaf child exists, return
    its ID (auto-resolve).  Otherwise, return an error listing available
    leaf children so the user can pick one.

    Returns the validated (possibly resolved) account ID.
    """
    q = (Q.from_(ACCOUNT)
         .select(ACCOUNT.id, ACCOUNT.name, ACCOUNT.account_number, ACCOUNT.is_group)
         .where(ACCOUNT.id == P()))
    row = conn.execute(q.get_sql(), (account_id,)).fetchone()
    if not row:
        return account_id  # Account existence is checked elsewhere

    if not row["is_group"]:
        return account_id  # Leaf account — OK

    # Group account — try to find leaf children
    q_children = (Q.from_(ACCOUNT)
                  .select(ACCOUNT.id, ACCOUNT.name, ACCOUNT.account_number)
                  .where(ACCOUNT.parent_id == P())
                  .where(ACCOUNT.is_group == 0)
                  .where(ACCOUNT.disabled == 0))
    children = conn.execute(q_children.get_sql(), (account_id,)).fetchall()

    if len(children) == 1:
        # Single leaf child — auto-resolve
        child = row_to_dict(children[0])
        return child["id"]

    if len(children) == 0:
        err(f"Account '{row['name']}' ({label}) is a group account with no "
            f"leaf children. Please create a child account under it first.")
    else:
        child_list = ", ".join(
            f"'{row_to_dict(c)['name']}' ({row_to_dict(c)['account_number'] or row_to_dict(c)['id']})"
            for c in children
        )
        err(f"Account '{row['name']}' ({label}) is a group account. "
            f"Cannot post to group accounts. "
            f"Please specify one of its leaf children: {child_list}")


# ---------------------------------------------------------------------------
# 1. add-payment
# ---------------------------------------------------------------------------

def add_payment(conn, args):
    """Create a new draft payment entry."""
    company_id = args.company_id
    if not company_id:
        err("--company-id is required")
    payment_type = args.payment_type
    if not payment_type or payment_type not in VALID_PAYMENT_TYPES:
        err(f"--payment-type is required. Valid: {VALID_PAYMENT_TYPES}")
    posting_date = args.posting_date
    if not posting_date:
        err("--posting-date is required")
    party_type = args.party_type
    if payment_type != "internal_transfer":
        if not party_type or not _party_type_registered(conn, party_type):
            err(f"--party-type is required and must be registered. Standard: {VALID_PARTY_TYPES}")
    party_id = args.party_id
    if payment_type != "internal_transfer" and not party_id:
        err("--party-id is required")
    paid_from = args.paid_from_account
    if not paid_from:
        err("--paid-from-account is required")
    paid_to = args.paid_to_account
    if not paid_to:
        err("--paid-to-account is required")
    paid_amount = args.paid_amount
    if not paid_amount:
        err("--paid-amount is required")

    # Validate company
    q = Q.from_(COMPANY).select(COMPANY.id).where(COMPANY.id == P())
    if not conn.execute(q.get_sql(), (company_id,)).fetchone():
        err(f"Company {company_id} not found")

    # Validate accounts exist and are not group accounts
    qa = Q.from_(ACCOUNT).select(ACCOUNT.id).where(ACCOUNT.id == P())
    for acct_id, label in [(paid_from, "paid-from-account"), (paid_to, "paid-to-account")]:
        if not conn.execute(qa.get_sql(), (acct_id,)).fetchone():
            err(f"Account {acct_id} ({label}) not found")

    # Resolve group accounts to leaf children (or error)
    paid_from = _validate_not_group_account(conn, paid_from, "paid-from-account")
    paid_to = _validate_not_group_account(conn, paid_to, "paid-to-account")

    amount = round_currency(to_decimal(paid_amount))
    if amount <= 0:
        err("--paid-amount must be > 0")

    exchange_rate = to_decimal(args.exchange_rate or "1")
    received_amount = round_currency(amount * exchange_rate)
    payment_currency = args.payment_currency or "USD"

    pe_id = str(uuid.uuid4())
    naming = get_next_name(conn, "payment_entry", company_id=company_id)

    sql, _ = insert_row("payment_entry", {
        "id": P(), "naming_series": P(), "payment_type": P(),
        "posting_date": P(), "party_type": P(), "party_id": P(),
        "paid_from_account": P(), "paid_to_account": P(),
        "paid_amount": P(), "received_amount": P(),
        "payment_currency": P(), "exchange_rate": P(),
        "reference_number": P(), "reference_date": P(),
        "status": P(), "unallocated_amount": P(), "company_id": P(),
    })
    conn.execute(sql,
        (pe_id, naming, payment_type, posting_date,
         party_type, party_id, paid_from, paid_to,
         str(amount), str(received_amount),
         payment_currency, str(exchange_rate),
         args.reference_number, args.reference_date,
         "draft", str(amount),  # unallocated = full amount initially
         company_id))

    # Insert allocations if provided
    if args.allocations:
        try:
            allocs = json.loads(args.allocations) if isinstance(args.allocations, str) else args.allocations
        except json.JSONDecodeError as e:
            err("Invalid JSON format in --allocations")
        _insert_allocations(conn, pe_id, allocs)

    # WS2 D3: insert deductions if provided (the non-cash slice of paid_amount).
    # getattr: older callers build Namespaces without the flag.
    deductions_arg = getattr(args, "deductions", None)
    total_deducted = Decimal("0")
    if deductions_arg:
        total_deducted = _insert_deductions(conn, pe_id, payment_type, deductions_arg)

    if args.allocations or deductions_arg:
        _recalc_unallocated(conn, pe_id)

    # A deduction must never push the remainder negative: enforce
    # paid_amount = allocations + deductions + unallocated with unallocated >= 0.
    if total_deducted > 0:
        qu = Q.from_(PE).select(PE.unallocated_amount).where(PE.id == P())
        urow = conn.execute(qu.get_sql(), (pe_id,)).fetchone()
        if to_decimal(urow["unallocated_amount"]) < 0:
            err("Allocations plus deductions exceed --paid-amount "
                "(paid_amount = allocations + deductions + unallocated)")

    audit(conn, "erpclaw-payments", "add-payment", "payment_entry", pe_id,
           new_values={"naming_series": naming, "payment_type": payment_type,
                       "paid_amount": str(amount)})
    conn.commit()

    ok({"status": "created", "payment_entry_id": pe_id,
         "naming_series": naming})


# ---------------------------------------------------------------------------
# 2. update-payment
# ---------------------------------------------------------------------------

def update_payment(conn, args):
    """Update a draft payment entry.

    THE RESIDUAL IS RECOMPUTED FROM THE DETAIL ROWS ON EVERY MONEY EDIT (M60 /
    F21-FINDING-1). ``--paid-amount`` used to write paid_amount + received_amount
    and stop there, so the payment kept the residual of the OLD amount and

        paid_amount = Σ live allocations + Σ deductions + unallocated

    stopped holding — the identity INV-27 audits from the ledger side once the
    payment is submitted. Measured before the fix (SIM
    ``planning/simlogs/m60_SIM_2026-08-12.md`` §2): 1,000.00 with a 300.00
    allocation reduced to 500.00 kept a 700.00 residual, and the break did NOT
    require an allocation to exist — a bare 400.00 → 900.00 edit left the
    residual at 400.00.

    Two shape decisions come from that SIM, both measured rather than assumed:

    1. ONE recompute site, after every branch, not one per branch. The
       allocations branch already recomputed; ``--paid-amount`` and
       ``--allocations`` in the SAME call was therefore already correct, because
       the allocations recompute ran last. Recomputing per-branch would have
       needed the same call ordered the same way to stay correct; recomputing
       once at the end makes the order irrelevant.
    2. The non-negative guard reads the POST-change detail rows. Comparing the
       new paid_amount against the CURRENT allocations would refuse
       ``--paid-amount 200 --allocations [100]`` on a 300-allocated draft, which
       is a legal edit that works today.

    Draft-only, and that guard is load-bearing rather than incidental: the
    F2 party-level compensation (``_post_party_residual_compensation``) is
    computed from allocation/deduction DETAIL and never reads paid_amount, so a
    post-submit paid_amount edit would move INV-27's RHS while its LHS stood
    still, and the GL entries already posted at the old amount would stop
    describing the document. Nothing here can reach a non-draft row.
    """
    pe_id = args.payment_entry_id
    if not pe_id:
        err("--payment-entry-id is required")

    pe = _get_pe_or_err(conn, pe_id)
    if pe["status"] != "draft":
        err(f"Cannot update: payment is '{pe['status']}' (must be 'draft')",
             suggestion="Cancel the document first, then make changes.")

    updated_fields = []
    old_values = {}

    if args.paid_amount:
        amount = round_currency(to_decimal(args.paid_amount))
        if amount <= 0:
            err("--paid-amount must be > 0")
        old_values["paid_amount"] = pe["paid_amount"]
        exchange_rate = to_decimal(pe["exchange_rate"])
        received = round_currency(amount * exchange_rate)
        sql = update_row("payment_entry",
                         data={"paid_amount": P(), "received_amount": P(),
                               "updated_at": now()},
                         where={"id": P()})
        conn.execute(sql, (str(amount), str(received), pe_id))
        updated_fields.append("paid_amount")

    if args.reference_number is not None:
        old_values["reference_number"] = pe["reference_number"]
        sql = update_row("payment_entry",
                         data={"reference_number": P(),
                               "updated_at": now()},
                         where={"id": P()})
        conn.execute(sql, (args.reference_number, pe_id))
        updated_fields.append("reference_number")

    if args.allocations:
        try:
            allocs = json.loads(args.allocations) if isinstance(args.allocations, str) else args.allocations
        except json.JSONDecodeError as e:
            err("Invalid JSON format in --allocations")
        dq = Q.from_(PA).delete().where(PA.payment_entry_id == P())
        conn.execute(dq.get_sql(), (pe_id,))
        _insert_allocations(conn, pe_id, allocs)
        updated_fields.append("allocations")

    if not updated_fields:
        err("No fields to update")

    # M60: the single recompute site. Reached whenever either side of the
    # identity moved — the paid_amount term or the allocation terms.
    #
    # The non-negative guard is scoped to the paid_amount edit, which is M60's
    # plan row, and deliberately not widened here. The rest of this module
    # already accepts a negative residual whenever no deduction is present:
    # add_payment's identical check is gated on `total_deducted > 0` (measured:
    # add-payment 100.00 with a 300.00 allocation returns ok with a −200.00
    # residual) and submit_payment carries the same gate, so the guard is
    # currently unreachable without a deduction on every one of those paths.
    # That is one defect class of its own, it reaches GL posting, and it needs
    # its own row rather than a silent expansion from this branch. Recorded in
    # planning/simlogs/m60_SIM_2026-08-12.md §5.
    if "paid_amount" in updated_fields or "allocations" in updated_fields:
        residual = _recalc_unallocated(conn, pe_id)
        if "paid_amount" in updated_fields and residual < 0:
            _refuse_negative_residual(conn, pe_id, residual)

    audit(conn, "erpclaw-payments", "update-payment", "payment_entry", pe_id,
           old_values=old_values, new_values={"updated_fields": updated_fields})
    conn.commit()

    ok({"status": "updated", "payment_entry_id": pe_id,
         "updated_fields": updated_fields})


# ---------------------------------------------------------------------------
# 3. get-payment
# ---------------------------------------------------------------------------

def get_payment(conn, args):
    """Get a payment entry with allocations."""
    pe_id = args.payment_entry_id
    if not pe_id:
        err("--payment-entry-id is required")

    pe = _get_pe_or_err(conn, pe_id)
    allocs = _get_allocations(conn, pe_id)
    deds = _get_deductions(conn, pe_id)

    formatted_allocs = [{
        "id": a["id"],
        "voucher_type": a["voucher_type"],
        "voucher_id": a["voucher_id"],
        "allocated_amount": a["allocated_amount"],
        "exchange_gain_loss": a.get("exchange_gain_loss", "0"),
    } for a in allocs]
    formatted_deds = [{
        "id": d["id"],
        "account_id": d["account_id"],
        "amount": d["amount"],
        "type": d["type"],
        "description": d.get("description"),
    } for d in deds]

    ok({
        "id": pe["id"],
        "naming_series": pe["naming_series"],
        "payment_type": pe["payment_type"],
        "posting_date": pe["posting_date"],
        "party_type": pe["party_type"],
        "party_id": pe["party_id"],
        "paid_from_account": pe["paid_from_account"],
        "paid_to_account": pe["paid_to_account"],
        "paid_amount": pe["paid_amount"],
        "received_amount": pe["received_amount"],
        "payment_currency": pe["payment_currency"],
        "exchange_rate": pe["exchange_rate"],
        "reference_number": pe.get("reference_number"),
        "reference_date": pe.get("reference_date"),
        "status": pe["status"],
        "unallocated_amount": pe["unallocated_amount"],
        "company_id": pe["company_id"],
        "allocations": formatted_allocs,
        "deductions": formatted_deds,
    })


# ---------------------------------------------------------------------------
# 4. list-payments
# ---------------------------------------------------------------------------

def list_payments(conn, args):
    """List payment entries with filtering."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    pe = Table("payment_entry")
    base = Q.from_(pe).where(pe.company_id == P())
    params = [company_id]

    if args.payment_type:
        base = base.where(pe.payment_type == P())
        params.append(args.payment_type)
    if args.party_type:
        base = base.where(pe.party_type == P())
        params.append(args.party_type)
    if args.party_id:
        base = base.where(pe.party_id == P())
        params.append(args.party_id)
    if args.pe_status:
        base = base.where(pe.status == P())
        params.append(args.pe_status)
    if args.from_date:
        base = base.where(pe.posting_date >= P())
        params.append(args.from_date)
    if args.to_date:
        base = base.where(pe.posting_date <= P())
        params.append(args.to_date)

    count_q = base.select(fn.Count("*"))
    count_row = conn.execute(count_q.get_sql(), params).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    list_params = params + [limit, offset]

    data_q = (base.select(
                  pe.id, pe.naming_series, pe.payment_type, pe.posting_date,
                  pe.party_type, pe.party_id, pe.paid_amount, pe.status,
                  pe.unallocated_amount)
              .orderby(pe.posting_date, order=Order.desc)
              .orderby(pe.created_at, order=Order.desc))
    # Add party_name resolution via CASE subquery
    sql = data_q.get_sql()
    # Insert party_name subquery after SELECT columns
    party_name_sql = (
        ",(CASE \"payment_entry\".\"party_type\" "
        "WHEN 'customer' THEN (SELECT \"name\" FROM \"customer\" WHERE \"id\"=\"payment_entry\".\"party_id\") "
        "WHEN 'supplier' THEN (SELECT \"name\" FROM \"supplier\" WHERE \"id\"=\"payment_entry\".\"party_id\") "
        "WHEN 'employee' THEN (SELECT \"full_name\" FROM \"employee\" WHERE \"id\"=\"payment_entry\".\"party_id\") "
        "ELSE \"payment_entry\".\"party_id\" END) AS \"party_name\""
    )
    # Insert before FROM clause
    sql = sql.replace(" FROM ", party_name_sql + " FROM ", 1)
    rows = conn.execute(
        sql + " LIMIT ? OFFSET ?", list_params
    ).fetchall()

    ok({"payments": [row_to_dict(r) for r in rows], "total_count": total_count,
         "limit": limit, "offset": offset,
         "has_more": offset + limit < total_count})


# ---------------------------------------------------------------------------
# 5. submit-payment
# ---------------------------------------------------------------------------

def _calc_early_payment_discount(conn, pe, allocations):
    """Check allocations for invoices eligible for early payment discount.

    Returns (total_discount, discount_account_id, discount_details).
    If no discount applies, returns (Decimal("0"), None, []).
    """
    from datetime import date as dt_date
    total_discount = Decimal("0")
    details = []
    discount_account_id = None

    payment_date = pe["posting_date"]
    try:
        pay_dt = dt_date.fromisoformat(payment_date)
    except (ValueError, TypeError):
        return Decimal("0"), None, []

    for alloc in allocations:
        vtype = alloc.get("voucher_type", "")
        vid = alloc.get("voucher_id", "")
        if vtype not in ("sales_invoice", "purchase_invoice"):
            continue

        if vtype == "sales_invoice":
            qi = Q.from_(SI).select(SI.posting_date, SI.payment_terms_id).where(SI.id == P())
            inv = conn.execute(qi.get_sql(), (vid,)).fetchone()
        else:
            qi = Q.from_(PI).select(PI.posting_date, PI.payment_terms_id).where(PI.id == P())
            inv = conn.execute(qi.get_sql(), (vid,)).fetchone()
        if not inv or not inv["payment_terms_id"]:
            continue

        qt = Q.from_(PT).select(PT.discount_percentage, PT.discount_days).where(PT.id == P())
        pt = conn.execute(qt.get_sql(), (inv["payment_terms_id"],)).fetchone()
        if not pt or not pt["discount_percentage"] or not pt["discount_days"]:
            continue

        disc_pct = to_decimal(pt["discount_percentage"])
        disc_days = int(pt["discount_days"])
        if disc_pct <= 0 or disc_days <= 0:
            continue

        try:
            inv_dt = dt_date.fromisoformat(inv["posting_date"])
        except (ValueError, TypeError):
            continue

        if (pay_dt - inv_dt).days <= disc_days:
            alloc_amt = to_decimal(alloc.get("allocated_amount", "0"))
            disc_amt = round_currency(alloc_amt * disc_pct / Decimal("100"))
            if disc_amt > 0:
                total_discount += disc_amt
                details.append({
                    "voucher_type": vtype, "voucher_id": vid,
                    "discount_percentage": str(disc_pct),
                    "discount_amount": str(disc_amt),
                })

    # Find discount account and default cost center
    cost_center_id = None
    if total_discount > 0:
        disc_name = "Sales Discounts" if pe["payment_type"] == "receive" else "Purchase Discounts"
        qa = (Q.from_(ACCOUNT).select(ACCOUNT.id)
              .where(ACCOUNT.name == P()).where(ACCOUNT.company_id == P()))
        acct = conn.execute(qa.get_sql(), (disc_name, pe["company_id"])).fetchone()
        if acct:
            discount_account_id = acct["id"]
        # Get default cost center for P&L tracking
        qc = (Q.from_(CC).select(CC.id)
              .where(CC.company_id == P()).where(CC.is_group == P()))
        cc = conn.execute(qc.get_sql() + " LIMIT 1", (pe["company_id"], 0)).fetchone()
        if cc:
            cost_center_id = cc["id"]

    return total_discount, discount_account_id, details, cost_center_id


def _assert_currency_match(conn, pe, allocations):
    """Enforce: invoice currency must equal payment currency.

    Per ERPClaw multi-currency rule (2026-04-27): we do not convert. If a
    customer invoice was raised in EUR, the payment matched against it
    must also be in EUR. Stripe and the cardholder bank handle FX before
    the money lands in our books.

    Walks the allocation list. For each allocation against a sales_invoice
    or purchase_invoice, looks up the invoice currency and compares it
    (case-insensitively) to the payment_entry payment_currency. On any
    mismatch, calls err() with a clean JSON error and exits 1.
    """
    pay_ccy = (pe.get("payment_currency") or "USD").upper()
    for alloc in allocations:
        vtype = alloc.get("voucher_type") or alloc.get("reference_type")
        vid = alloc.get("voucher_id") or alloc.get("reference_id")
        if not vtype or not vid:
            continue
        if vtype == "sales_invoice":
            row = conn.execute(
                Q.from_(SI).select(SI.currency).where(SI.id == P()).get_sql(),
                (vid,)
            ).fetchone()
        elif vtype == "purchase_invoice":
            row = conn.execute(
                Q.from_(PI).select(PI.currency).where(PI.id == P()).get_sql(),
                (vid,)
            ).fetchone()
        else:
            continue
        if not row:
            continue
        inv_ccy = (row["currency"] or "USD").upper()
        if inv_ccy != pay_ccy:
            err(
                f"currency mismatch: invoice in {inv_ccy}, payment in "
                f"{pay_ccy}; invoice currency must equal payment currency"
            )


def _resolve_advance_routing(conn, pe):
    """S2: if the company has an advance sub-account configured for this payment's
    direction, return (advance_account_id, control_account_id, side); else
    (None, None, None). side is the GL leg to split — 'credit' for a customer
    receive (the AR/paid_from leg), 'debit' for a supplier pay (the AP/paid_to leg)."""
    if pe["payment_type"] not in ("receive", "pay") or not pe["party_type"]:
        return None, None, None
    row = conn.execute(
        "SELECT advance_from_customer_account_id, advance_to_supplier_account_id "
        "FROM company WHERE id = ?", (pe["company_id"],)).fetchone()
    if not row:
        return None, None, None
    if pe["payment_type"] == "receive":
        return row["advance_from_customer_account_id"], pe["paid_from_account"], "credit"
    return row["advance_to_supplier_account_id"], pe["paid_to_account"], "debit"


def _route_advance_portion(gl_entries, control_acct, advance_acct, amount, side):
    """Move `amount` of the party control leg (on control_acct, on the given side)
    onto a new leg on advance_acct. Same side, so debits=credits is preserved.
    Mutates gl_entries in place. Returns True if a split was applied."""
    amount = round_currency(amount)
    for e in gl_entries:
        if e["account_id"] != control_acct:
            continue
        cur = to_decimal(e[side])
        if cur <= 0:
            continue
        if amount > cur:
            return False  # safety: never route more than the control leg holds
        remaining = round_currency(cur - amount)
        if remaining == 0:
            # the whole control leg is the advance — just repoint it
            e["account_id"] = advance_acct
        else:
            e[side] = str(remaining)
            adv = {"account_id": advance_acct, "debit": "0", "credit": "0",
                   "party_type": e.get("party_type"), "party_id": e.get("party_id")}
            adv[side] = str(amount)
            gl_entries.append(adv)
        return True
    return False


def submit_payment(conn, args):
    """Submit a draft payment: post GL entries, create PLE, update status.

    Automatically detects and applies early payment discounts when
    allocations reference invoices with payment terms that include
    discount_percentage and discount_days, and the payment is made
    within the discount window.
    """
    pe_id = args.payment_entry_id
    if not pe_id:
        err("--payment-entry-id is required")

    pe = _get_pe_or_err(conn, pe_id)
    if pe["status"] != "draft":
        err(f"Cannot submit: payment is '{pe['status']}' (must be 'draft')")

    # Pre-GL validation: ensure accounts are not group accounts
    # (catches cases where draft was created before this check existed)
    for acct_key, label in [("paid_from_account", "paid-from-account"),
                            ("paid_to_account", "paid-to-account")]:
        resolved = _validate_not_group_account(conn, pe[acct_key], label)
        if resolved != pe[acct_key]:
            # Auto-resolved to leaf child — update the payment entry
            sql = update_row("payment_entry",
                             data={acct_key: P(), "updated_at": now()},
                             where={"id": P()})
            conn.execute(sql, (resolved, pe_id))
            pe[acct_key] = resolved

    paid_amount = to_decimal(pe["paid_amount"])
    allocations = _get_allocations(conn, pe_id)

    # WS2 D3: deductions (non-cash slice of paid_amount). Re-validated here in
    # case rows landed via a path that skipped add-payment's guard.
    deductions = _get_deductions(conn, pe_id)
    total_deductions = sum((to_decimal(d["amount"]) for d in deductions),
                           Decimal("0"))
    if total_deductions > 0:
        if pe["payment_type"] == "internal_transfer":
            err("--deductions is not supported for internal_transfer payments")
        total_allocated = sum((to_decimal(a["allocated_amount"])
                               for a in allocations), Decimal("0"))
        if paid_amount - total_allocated - total_deductions < 0:
            err("Allocations plus deductions exceed paid amount "
                "(paid_amount = allocations + deductions + unallocated)")

    # Multi-currency rule (2026-04-27): invoice currency must equal payment
    # currency. Reject any cross-currency allocation up front before any GL
    # writes happen. ERPClaw does not convert.
    _assert_currency_match(conn, pe, allocations)

    # Check for early payment discount
    discount_amount, discount_account_id, discount_details, disc_cost_center = \
        _calc_early_payment_discount(conn, pe, allocations)

    # Effective amount hitting the bank is reduced by discount
    bank_amount = paid_amount - discount_amount
    receivable_amount = paid_amount  # Full amount clears the receivable

    # Build GL entries based on payment type
    # receive: DR paid_to (bank), CR paid_from (receivable)
    # pay: DR paid_to (payable), CR paid_from (bank)
    # internal_transfer: DR paid_to (bank), CR paid_from (bank)
    if discount_amount > 0 and discount_account_id:
        # With discount: bank gets less, discount account absorbs the rest
        disc_entry = {"account_id": discount_account_id,
                      "debit": str(discount_amount), "credit": "0",
                      "party_type": None, "party_id": None}
        if disc_cost_center:
            disc_entry["cost_center_id"] = disc_cost_center
        gl_entries = [
            {"account_id": pe["paid_to_account"], "debit": str(bank_amount), "credit": "0",
             "party_type": pe["party_type"], "party_id": pe["party_id"]},
            disc_entry,
            {"account_id": pe["paid_from_account"], "debit": "0", "credit": str(receivable_amount),
             "party_type": pe["party_type"], "party_id": pe["party_id"]},
        ]
    else:
        gl_entries = [
            {"account_id": pe["paid_to_account"], "debit": str(paid_amount), "credit": "0",
             "party_type": pe["party_type"], "party_id": pe["party_id"]},
            {"account_id": pe["paid_from_account"], "debit": "0", "credit": str(paid_amount),
             "party_type": pe["party_type"], "party_id": pe["party_id"]},
        ]

    # WS2 D3: deduction legs. The cash leg shrinks by the deducted total while
    # the party control leg keeps the FULL paid_amount (invoice-clearing legs
    # unchanged), so debits still equal credits and the 12-step validation below
    # sees the final set. Independent of the S2 advance routing that follows —
    # deductions touch the CASH leg, advance routing touches the CONTROL leg.
    if total_deductions > 0:
        _apply_deduction_legs(conn, pe, gl_entries, deductions, total_deductions)

    # S2: route the unallocated (advance) portion of the party control leg to a
    # dedicated advance sub-account if the company configured one. The advance is
    # a liability (customer) / asset (supplier) until applied to an invoice; the
    # offsetting reclassification is posted by allocate-payment. Backward-compatible:
    # no config -> unchanged (whole amount stays on the AR/AP control account).
    routed_advance_account = None
    _adv_acct, _ctrl_acct, _adv_side = _resolve_advance_routing(conn, pe)
    if _adv_acct:
        _U = to_decimal(pe["unallocated_amount"])
        if _U > 0 and _route_advance_portion(gl_entries, _ctrl_acct, _adv_acct, _U, _adv_side):
            routed_advance_account = _adv_acct

    # Apply multi-currency: set currency/exchange_rate on GL entries.
    # Per the 2026-04-27 scope rule (invoice currency == payment currency,
    # no FX conversion), payment_rate is always Decimal("1") in production.
    # The branch below that fires when rate != 1, and the FX gain/loss
    # branch further down, are dormant under the current rule. They are
    # kept in place because the underlying schema and helpers still work
    # if we ever expand scope to actual cross-currency conversion.
    payment_currency = pe["payment_currency"] or "USD"
    payment_rate = to_decimal(pe["exchange_rate"] or "1")
    if payment_currency != "USD" or payment_rate != Decimal("1"):
        prepare_multicurrency_entries(gl_entries, payment_currency, payment_rate)

    # DORMANT under current scope: FX gain/loss only fires on rate != 1.
    # Currency-match validation above guarantees rate == 1 for now.
    fx_gain_loss_total = Decimal("0")
    if allocations and payment_rate != Decimal("1"):
        qc = Q.from_(COMPANY).select(COMPANY.exchange_gain_loss_account_id).where(COMPANY.id == P())
        company = conn.execute(qc.get_sql(), (pe["company_id"],)).fetchone()
        fx_account_id = company["exchange_gain_loss_account_id"] if company else None

        for alloc in allocations:
            inv_rate = Decimal("1")
            # Try to get original invoice exchange rate
            if alloc.get("reference_type") == "sales_invoice":
                qi = Q.from_(SI).select(SI.exchange_rate).where(SI.id == P())
                inv_row = conn.execute(qi.get_sql(), (alloc["reference_id"],)).fetchone()
                if inv_row and inv_row["exchange_rate"]:
                    inv_rate = to_decimal(inv_row["exchange_rate"])
            elif alloc.get("reference_type") == "purchase_invoice":
                qi = Q.from_(PI).select(PI.exchange_rate).where(PI.id == P())
                inv_row = conn.execute(qi.get_sql(), (alloc["reference_id"],)).fetchone()
                if inv_row and inv_row["exchange_rate"]:
                    inv_rate = to_decimal(inv_row["exchange_rate"])

            if inv_rate != payment_rate:
                alloc_amount = to_decimal(alloc["allocated_amount"])
                gl = calculate_exchange_gain_loss(
                    alloc_amount, payment_rate, inv_rate
                )
                fx_gain_loss_total += gl
                # Update allocation record
                sql = update_row("payment_allocation",
                                 data={"exchange_gain_loss": P()},
                                 where={"id": P()})
                conn.execute(sql, (str(gl), alloc["id"]))

        # Post FX gain/loss GL entries if there's a net amount
        if fx_gain_loss_total != 0 and fx_account_id:
            post_exchange_gain_loss(
                gl_entries, fx_gain_loss_total, fx_account_id
            )
            # FX entry needs a cost center for P&L tracking
            if gl_entries[-1].get("account_id") == fx_account_id:
                # Use the first cost center found, or look up default
                qcc = Q.from_(COMPANY).select(COMPANY.default_cost_center_id).where(COMPANY.id == P())
                default_cc = conn.execute(qcc.get_sql(), (pe["company_id"],)).fetchone()
                if default_cc and default_cc["default_cost_center_id"]:
                    gl_entries[-1]["cost_center_id"] = default_cc["default_cost_center_id"]
                # Also need to add offsetting base amount difference to AR/AP entry
                # The prepare_multicurrency_entries already handled base amounts

    try:
        validate_gl_entries(
            conn, gl_entries, pe["company_id"],
            pe["posting_date"], voucher_type="payment_entry",
        )
        gl_ids = insert_gl_entries(
            conn, gl_entries,
            voucher_type="payment_entry",
            voucher_id=pe_id,
            posting_date=pe["posting_date"],
            company_id=pe["company_id"],
            remarks=f"Payment {pe['naming_series']}",
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-payments] {e}\n")
        err(f"GL posting failed: {e}")

    # Create payment ledger entry (tracks outstanding)
    ple_id = str(uuid.uuid4())
    # For receive: negative PLE (reduces receivable outstanding)
    # For pay: negative PLE (reduces payable outstanding)
    ple_amount = str(round_currency(-paid_amount))
    if pe["party_type"] and pe["party_id"]:
        # Determine the account for PLE (receivable for receive, payable for pay)
        ple_account = pe["paid_from_account"] if pe["payment_type"] == "receive" else pe["paid_to_account"]
        ple_sql, _ = insert_row("payment_ledger_entry", {
            "id": P(), "posting_date": P(), "account_id": P(),
            "party_type": P(), "party_id": P(),
            "voucher_type": P(), "voucher_id": P(),
            "amount": P(), "amount_in_account_currency": P(),
            "currency": P(), "remarks": P(),
        })
        conn.execute(ple_sql,
            (ple_id, pe["posting_date"], ple_account,
             pe["party_type"], pe["party_id"],
             "payment_entry", pe_id, ple_amount, ple_amount,
             pe["payment_currency"],
             f"Payment {pe['naming_series']}"))

    sql = update_row("payment_entry",
                     data={"status": P(), "updated_at": now()},
                     where={"id": P()})
    conn.execute(sql, ("submitted", pe_id))

    # Wave G F2 (M38), correction C3 — lifecycle site 1. The party-level row
    # above subtracts the FULL paid_amount while the per-allocation rows below
    # subtract the same cash again; the compensation converges the party back to
    # the truth. It runs HERE, immediately after the party-level row and the
    # status flip (the helper's guard reads the stored status, so the flip has to
    # land first) and inside this same transaction. Delta 0 writes nothing, so an
    # advance with no allocations and no deductions appends no row at all.
    _post_party_residual_compensation(conn, pe_id)

    # S2: record that the advance portion was routed to a sub-account so
    # allocate-payment knows to post the offsetting reclassification.
    if routed_advance_account:
        conn.execute(
            "UPDATE payment_entry SET advance_account_id = ? WHERE id = ?",
            (routed_advance_account, pe_id))
        pe["advance_account_id"] = routed_advance_account

    # Clear each pre-existing invoice allocation: sync the document's
    # outstanding/status AND post the per-allocation PLE that offsets the
    # invoice's voucher PLE (INV-22). Allocations created LATER via
    # allocate-payment / reconcile-payments are cleared at those sites — each
    # allocation is processed exactly once.
    # WS2 D3: deductions ride the invoice clearing. The control leg was posted
    # for the FULL paid_amount, so each invoice must be cleared by its allocation
    # PLUS its pro-rata deduction share or AR/AP GL and the subledger diverge
    # (a $980 wire + $20 discount clears a $1,000 invoice completely). Shares
    # distribute over the submit-time invoice allocations only.
    ded_shares = _deduction_shares(allocations, total_deductions)
    docs_cleared = 0
    invoice_allocations = 0
    try:
        for alloc in allocations:
            # Canonical view of the allocation's voucher type (a pre-existing
            # row could still carry a label form; new rows are stored canonical).
            vt = canonical_voucher_type(alloc["voucher_type"])
            if vt in INVOICE_VOUCHER_TYPES:
                invoice_allocations += 1
            effective = round_currency(
                to_decimal(alloc["allocated_amount"])
                + ded_shares.get(alloc["id"], Decimal("0")))
            if _clear_invoice_allocation(
                    conn, pe, alloc["voucher_type"], alloc["voucher_id"],
                    effective):
                docs_cleared += 1
    except ValueError as e:
        conn.rollback()
        sys.stderr.write(f"[erpclaw-payments] {e}\n")
        err(f"Payment allocation failed: {e}")

    # Guard against a silent false success: if the payment named invoice-like
    # allocations but none actually cleared, do not pretend the books moved.
    # (A payment with only advance / on-account allocations legitimately clears
    # nothing — invoice_allocations == 0 — and is not an error.)
    if invoice_allocations > 0 and docs_cleared == 0:
        conn.rollback()
        sys.stderr.write(
            "[erpclaw-payments] payment named invoice allocations but cleared "
            "no document\n")
        err("Payment named invoice allocations but cleared no document")

    result = {"status": "submitted", "payment_entry_id": pe_id,
              "gl_entries_created": len(gl_ids),
              "documents_cleared": docs_cleared,
              "outstanding_updated": docs_cleared > 0}
    if discount_amount > 0:
        result["early_payment_discount"] = {
            "discount_amount": str(discount_amount),
            "bank_amount": str(bank_amount),
            "details": discount_details,
        }
    if total_deductions > 0:
        result["deductions"] = {
            "total": str(round_currency(total_deductions)),
            "count": len(deductions),
        }
    if fx_gain_loss_total != 0:
        result["exchange_gain_loss"] = str(round_currency(fx_gain_loss_total))

    audit(conn, "erpclaw-payments", "submit-payment", "payment_entry", pe_id,
           new_values={"gl_entries_created": len(gl_ids),
                       "discount_amount": str(discount_amount)})
    conn.commit()

    ok(result)


# ---------------------------------------------------------------------------
# 6. cancel-payment
# ---------------------------------------------------------------------------

def cancel_payment(conn, args):
    """Cancel a submitted payment: reverse GL entries, reverse PLE."""
    pe_id = args.payment_entry_id
    if not pe_id:
        err("--payment-entry-id is required")

    pe = _get_pe_or_err(conn, pe_id)
    if pe["status"] != "submitted":
        err(f"Cannot cancel: payment is '{pe['status']}' (must be 'submitted')")

    # Reverse GL entries
    try:
        reverse_gl_entries(
            conn,
            voucher_type="payment_entry",
            voucher_id=pe_id,
            posting_date=pe["posting_date"],
        )
    except ValueError as e:
        sys.stderr.write(f"[erpclaw-payments] {e}\n")
        err(f"GL reversal failed: {e}")

    # Undo document clearing (Part 1): restore each cleared invoice's
    # outstanding + status. Amounts come from the per-allocation PLE rows, NOT
    # the allocation rows — the PLE records EXACTLY what was applied per invoice
    # (allocation + any deduction share, WS2 D3) across submit/allocate/
    # reconcile, so deduction legs ride the same voucher reversal. An invoice
    # still partially paid by OTHER payments stays partially_paid; one fully
    # un-paid returns to submitted.
    q_applied = (Q.from_(PLE)
                 .select(PLE.against_voucher_type, PLE.against_voucher_id,
                         PLE.amount)
                 .where(PLE.voucher_type == P())
                 .where(PLE.voucher_id == P())
                 .where(PLE.delinked == P())
                 .orderby(PLE.created_at).orderby(PLE.id))
    applied_by_doc = {}
    for row in conn.execute(q_applied.get_sql(), ("payment_entry", pe_id, 0)):
        avt = canonical_voucher_type(row["against_voucher_type"])
        avid = row["against_voucher_id"]
        if avt not in INVOICE_VOUCHER_TYPES or not avid:
            continue
        applied_by_doc[(avt, avid)] = (
            applied_by_doc.get((avt, avid), Decimal("0"))
            - to_decimal(row["amount"]))  # PLE rows are negative → negate
    for (vt, vid), applied in applied_by_doc.items():
        if applied <= 0:
            continue
        doc_t = SI if vt == "sales_invoice" else PI
        gt_q = Q.from_(doc_t).select(doc_t.grand_total).where(doc_t.id == P())
        gt_row = conn.execute(gt_q.get_sql(), (vid,)).fetchone()
        if gt_row is None:
            continue
        reverse_payment_on_document(
            conn, vt, vid, str(round_currency(applied)), gt_row["grand_total"])

    # Reverse PLE: mark existing as delinked, create offsetting entry. This
    # selects ALL non-delinked PLE rows for this payment — the party-level row
    # AND the per-allocation rows (Part 2) — so cancel nets every leg back.
    q_ple = (Q.from_(PLE).select(PLE.star)
             .where(PLE.voucher_type == P())
             .where(PLE.voucher_id == P())
             .where(PLE.delinked == P()))
    ple_rows = conn.execute(q_ple.get_sql(), ("payment_entry", pe_id, 0)).fetchall()
    delink_sql = update_row("payment_ledger_entry",
                            data={"delinked": P(), "updated_at": now()},
                            where={"id": P()})
    # Forward against_voucher_type/id onto the reversing row so per-allocation
    # reversals stay attributable and net INV-22 back correctly.
    ple_ins_sql, _ = insert_row("payment_ledger_entry", {
        "id": P(), "posting_date": P(), "account_id": P(),
        "party_type": P(), "party_id": P(),
        "voucher_type": P(), "voucher_id": P(),
        "against_voucher_type": P(), "against_voucher_id": P(),
        "amount": P(), "amount_in_account_currency": P(),
        "currency": P(), "remarks": P(),
    })
    for ple in ple_rows:
        ple_dict = row_to_dict(ple)
        conn.execute(delink_sql, (1, ple_dict["id"]))
        # Create reversing PLE
        reversal_amount = str(round_currency(-to_decimal(ple_dict["amount"])))
        conn.execute(ple_ins_sql,
            (str(uuid.uuid4()), pe["posting_date"], ple_dict["account_id"],
             ple_dict["party_type"], ple_dict["party_id"],
             "payment_entry", pe_id,
             ple_dict.get("against_voucher_type"), ple_dict.get("against_voucher_id"),
             reversal_amount, reversal_amount,
             ple_dict["currency"],
             f"Reversal: Payment {pe['naming_series']}"))

    sql = update_row("payment_entry",
                     data={"status": P(), "updated_at": now()},
                     where={"id": P()})
    conn.execute(sql, ("cancelled", pe_id))

    audit(conn, "erpclaw-payments", "cancel-payment", "payment_entry", pe_id,
           new_values={"reversed": True})
    conn.commit()

    ok({"status": "cancelled", "payment_entry_id": pe_id, "reversed": True})


# ---------------------------------------------------------------------------
# 7. delete-payment
# ---------------------------------------------------------------------------

def delete_payment(conn, args):
    """Delete a draft payment. Only drafts can be deleted."""
    pe_id = args.payment_entry_id
    if not pe_id:
        err("--payment-entry-id is required")

    pe = _get_pe_or_err(conn, pe_id)
    if pe["status"] != "draft":
        err(f"Cannot delete: payment is '{pe['status']}' (only 'draft' can be deleted)",
             suggestion="Cancel the document first, then delete.")

    naming = pe["naming_series"]
    conn.execute(Q.from_(PA).delete().where(PA.payment_entry_id == P()).get_sql(), (pe_id,))
    conn.execute(Q.from_(PD).delete().where(PD.payment_entry_id == P()).get_sql(), (pe_id,))
    conn.execute(Q.from_(PE).delete().where(PE.id == P()).get_sql(), (pe_id,))

    audit(conn, "erpclaw-payments", "delete-payment", "payment_entry", pe_id,
           old_values={"naming_series": naming})
    conn.commit()

    ok({"status": "deleted", "deleted": True})


# ---------------------------------------------------------------------------
# 7b. write-off-invoice  (Wave G F17a — its OWN clearing primitive)
# ---------------------------------------------------------------------------

# The GL entry_set the write-off pair is filed under, beside the invoice's own
# 'primary' (and 'cogs') sets on the SAME voucher. Chosen over a separate
# voucher for one reason that does all the work: reverse_gl_entries() reverses
# EVERY active entry for a (voucher_type, voucher_id) regardless of entry_set,
# and cancel-sales-invoice / cancel-purchase-invoice already delink every PLE
# row for that voucher — so a written-off invoice cancels correctly with ZERO
# new logic on the cancel side (ADR-0032 W18; SIM-0 S5 proved the shape).
WRITE_OFF_ENTRY_SET = "write_off"

# (voucher_type -> (party column, party_type, is the control leg credited?)).
# 'receivable is credited' on the AR side (the customer owes less), 'payable is
# debited' on the AP side (we owe less). Returns (credit/debit notes) are NOT
# write-off-able: their outstanding is negative by design, so "writing one off"
# is not an operation with a meaning — it is refused by name rather than
# producing a backwards GL pair.
_WRITE_OFF_DOCS = {
    "sales_invoice": ("customer_id", "customer", True),
    "purchase_invoice": ("supplier_id", "supplier", False),
}


def _active_write_off_gl(conn, voucher_type, voucher_id):
    """The active write-off GL set for a voucher, or None.

    insert_gl_entries() is idempotent per (voucher_type, voucher_id, entry_set),
    so a second write-off on the same invoice would die inside the shared lib
    with a generic 'GL entries already exist' message. This surfaces the real
    reason to the operator instead, with the amount already written off.
    """
    row = conn.execute(
        "SELECT decimal_sum(debit) AS dr, decimal_sum(credit) AS cr, "
        "       COUNT(*) AS cnt "
        "  FROM gl_entry "
        " WHERE voucher_type = ? AND voucher_id = ? AND entry_set = ? "
        "   AND is_cancelled = 0",
        (voucher_type, voucher_id, WRITE_OFF_ENTRY_SET)).fetchone()
    if not row or not row["cnt"]:
        return None
    return round_currency(to_decimal(row["dr"] or "0"))


def write_off_invoice(conn, args):
    """Write off part or all of ONE open invoice's outstanding balance.

    Wave G F17a (ruling N6a — in the correctness floor, un-gated, single
    invoice, explicit amount). This is its OWN clearing primitive and writes
    **no payment_entry and no payment_deduction row**: the shipped payment path
    structurally refuses a zero-cash write-off (``--paid-amount must be > 0``,
    the non-negative residual gate, and ``payment_deduction.payment_entry_id``
    being NOT NULL), so routing a write-off through it was unbuildable rather
    than merely awkward (Wave-G SIM finding 4 / plan correction C4).

    What it writes, all inside ONE transaction:

      1. A balanced GL pair under the **invoice's own voucher** with
         ``entry_set = 'write_off'``:
           AR: DR bad-debt expense / CR the invoice's receivable control
           AP: DR the invoice's payable control / CR the write-off account
         The control leg carries the party, exactly as the invoice's own
         control leg does, so party GL and party subledger move together.
      2. ONE payment_ledger_entry row under that same voucher
         (``against_voucher_*`` omitted, ``amount = −write_off``). Under
         erpclaw_lib.party_ledger's rules a document row buckets to its own
         voucher and carries the ``delinked = 0`` liveness filter, so INV-25
         sees outstanding and PLE net fall by the same amount on the document
         branch, and INV-27 falls by that amount on BOTH of its sides.
      3. The outstanding reduction + status sync, through the shared clearing
         lib (``apply_payment_to_document``) — the same canonical rule
         submit-payment and update-invoice-outstanding use, so a full-residual
         write-off reaches 'paid' by the existing sync with no bespoke branch.

    Unlike the GL-exempt PLE writers (ADR-0032 W8/W9/W11-W15/W17) this ledger
    row DOES have matching GL, so a future GL≡subledger check must expect to
    find it on both sides. That is W18's whole disposition.

    NOT in scope by ruling (N6 STRICT, restated at plan §2.3 and §4 F17): no
    batch write-off, no due-date sweep, no policy rules, no dunning hook. One
    invoice, one explicit amount, one reason.
    """
    voucher_type = canonical_voucher_type(args.voucher_type or "sales_invoice")
    if voucher_type not in _WRITE_OFF_DOCS:
        err(f"--voucher-type must be one of {tuple(_WRITE_OFF_DOCS)} "
            f"(got '{voucher_type}')",
            suggestion="Write off the accounting invoice, not a payment or a "
                       "credit/debit note.")
    voucher_id = args.voucher_id
    if not voucher_id:
        err("--voucher-id is required (the invoice being written off)")
    if not args.write_off_amount:
        err("--write-off-amount is required")
    try:
        amount = round_currency(to_decimal(args.write_off_amount))
    except (TypeError, ValueError, InvalidOperation):
        err(f"Invalid --write-off-amount {args.write_off_amount!r} "
            "(pass money as a string, e.g. \"340.00\", never a float)")
    if amount <= 0:
        err("--write-off-amount must be > 0")
    if not args.write_off_account_id:
        err("--write-off-account-id is required "
            "(the bad-debt expense account the write-off is charged to)")
    reason = (args.reason or "").strip()
    if not reason:
        err("--reason is required (a write-off is an accounting decision; the "
            "audit trail records why)")

    party_col, party_type, credit_control = _WRITE_OFF_DOCS[voucher_type]
    doc_t = Table(voucher_type)
    inv = conn.execute(
        Q.from_(doc_t).select(
            Field("id"), Field(party_col), Field("posting_date"),
            Field("outstanding_amount"), Field("status"), Field("is_return"),
            Field("company_id"))
        .where(Field("id") == P()).get_sql(), (voucher_id,)).fetchone()
    if inv is None:
        err(f"{voucher_type} {voucher_id} not found")
    if inv["is_return"]:
        err(f"{voucher_type} {voucher_id} is a return (credit/debit note); its "
            "outstanding is negative by design and cannot be written off",
            suggestion="Write off the original invoice instead.")
    outstanding = to_decimal(inv["outstanding_amount"])

    already = _active_write_off_gl(conn, voucher_type, voucher_id)
    if already is not None:
        err(f"{voucher_type} {voucher_id} already carries a write-off of "
            f"{already}. One write-off per invoice is supported "
            f"(GL entry_set '{WRITE_OFF_ENTRY_SET}' is posted once per voucher).",
            suggestion="Cancel the invoice to reverse the existing write-off, "
                       "or raise a credit note for the remaining balance.")

    # Same precondition update-invoice-outstanding enforces (INV-25): this action
    # moves the SUMMARY, so it must move the DETAIL, and it reuses the invoice's
    # own posting-time ledger row for account + currency so the write-off lands
    # in the same bucket on the same control account. Read BEFORE any write, so a
    # broken-ledger invoice errors with zero writes.
    src_ple = conn.execute(
        Q.from_(PLE).select(PLE.account_id, PLE.currency)
        .where(PLE.voucher_type == P()).where(PLE.voucher_id == P())
        .where(PLE.delinked == P())
        .orderby(PLE.created_at).orderby(PLE.id).get_sql() + " LIMIT 1",
        (voucher_type, voucher_id, 0)).fetchone()
    if src_ple is None:
        err(f"{voucher_type} {voucher_id} has no active payment ledger posting; "
            "cannot write it off (summary and detail must move together, INV-25)")
    control_account = src_ple["account_id"]

    write_off_account = _validate_not_group_account(
        conn, args.write_off_account_id, "write-off-account")
    acct = conn.execute(
        Q.from_(ACCOUNT).select(ACCOUNT.id, ACCOUNT.root_type)
        .where(ACCOUNT.id == P()).get_sql(), (write_off_account,)).fetchone()
    if acct is None:
        err(f"Write-off account {args.write_off_account_id} not found")
    if write_off_account == control_account:
        err("The write-off account must differ from the invoice's control "
            f"account ({control_account}); charging the write-off to the same "
            "account would post a self-cancelling pair and move nothing")

    # A write-off is dated by the DECISION, not by the invoice (QA condition 3,
    # pm ruling 2026-08-08). Defaulting to the invoice's own date backdated the
    # bad-debt expense into the period the sale was made: a 2026 collections
    # review would restate 2025, and once that year is closed the write-off
    # becomes impossible through the documented flag set. Today also matches
    # F17b, where the payment-time write-off lands on the payment's date.
    # `--posting-date` remains the explicit override for a deliberate backdate.
    posting_date = args.posting_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    amount_str = str(amount)
    control_leg = {"account_id": control_account,
                   "debit": "0" if credit_control else amount_str,
                   "credit": amount_str if credit_control else "0",
                   "party_type": party_type, "party_id": inv[party_col]}
    write_off_leg = {"account_id": write_off_account,
                     "debit": amount_str if credit_control else "0",
                     "credit": "0" if credit_control else amount_str,
                     "party_type": None, "party_id": None}
    # P&L legs need a cost center (12-step validation step 6) — same resolution
    # _apply_deduction_legs uses for its deduction legs.
    if acct["root_type"] in ("income", "expense"):
        cc = args.cost_center_id or _default_cost_center(conn, inv["company_id"])
        if cc:
            write_off_leg["cost_center_id"] = cc
    fiscal_year = get_fiscal_year(conn, posting_date)
    gl_entries = [write_off_leg, control_leg] if credit_control \
        else [control_leg, write_off_leg]
    for leg in gl_entries:
        leg["fiscal_year"] = fiscal_year

    # Outstanding + status move FIRST, through the shared clearing lib. The lib
    # owns BOTH the clearable-status rule and the over-application reject;
    # restating either here would be a second copy of a rule this module
    # deliberately does not own (the same reason _recalc_unallocated delegates).
    # Running it first also means a bad status costs zero writes.
    try:
        res = apply_payment_to_document(conn, voucher_type, voucher_id, amount)
    except ValueError as e:
        conn.rollback()
        err(str(e))

    try:
        validate_gl_entries(conn, gl_entries, inv["company_id"], posting_date,
                            voucher_type=voucher_type)
        gl_ids = insert_gl_entries(
            conn, gl_entries,
            voucher_type=voucher_type, voucher_id=voucher_id,
            posting_date=posting_date, company_id=inv["company_id"],
            remarks=f"Write-off {voucher_type} {voucher_id}: {reason}",
            entry_set=WRITE_OFF_ENTRY_SET)
    except ValueError as e:
        conn.rollback()
        sys.stderr.write(f"[erpclaw-payments] {e}\n")
        err(f"GL posting failed: {e}")

    ple_id = str(uuid.uuid4())
    ple_amount = str(round_currency(-amount))
    ple_sql, _ = insert_row("payment_ledger_entry", {
        "id": P(), "posting_date": P(), "account_id": P(),
        "party_type": P(), "party_id": P(),
        "voucher_type": P(), "voucher_id": P(),
        "amount": P(), "amount_in_account_currency": P(),
        "currency": P(), "remarks": P(),
    })
    conn.execute(ple_sql,
        (ple_id, posting_date, control_account, party_type, inv[party_col],
         voucher_type, voucher_id, ple_amount, ple_amount,
         src_ple["currency"], f"Write-off: {reason}"))

    audit(conn, "erpclaw-payments", "write-off-invoice", voucher_type,
          voucher_id,
          old_values={"outstanding_amount": str(outstanding),
                      "status": inv["status"]},
          new_values={"write_off_amount": amount_str,
                      "write_off_account_id": write_off_account,
                      "outstanding_amount": res["outstanding_amount"],
                      "status": res["status"],
                      "payment_ledger_entry_id": ple_id},
          description=reason)
    conn.commit()

    ok({"status": "written_off",
        "voucher_type": voucher_type, "voucher_id": voucher_id,
        "write_off_amount": amount_str,
        "write_off_account_id": write_off_account,
        "outstanding_amount": res["outstanding_amount"],
        "invoice_status": res["status"],
        "gl_entries_created": len(gl_ids),
        "payment_ledger_entry_id": ple_id,
        "reason": reason})


# ---------------------------------------------------------------------------
# 8. create-payment-ledger-entry
# ---------------------------------------------------------------------------

def create_payment_ledger_entry(conn, args):
    """Create a PLE record. Called cross-skill by selling/buying on invoice submit."""
    voucher_type = args.voucher_type
    if not voucher_type:
        err("--voucher-type is required")
    # FINDING-006: canonicalize both doctype voucher_types at the write boundary
    # so payment_ledger_entry rows store snake_case (the gateway may hand labels
    # like "Sales Invoice"); PLE netting/outstanding compare against snake_case.
    voucher_type = canonical_voucher_type(voucher_type)
    against_voucher_type = canonical_voucher_type(args.against_voucher_type)
    voucher_id = args.voucher_id
    if not voucher_id:
        err("--voucher-id is required")
    party_type = args.party_type
    if not party_type or not _party_type_registered(conn, party_type):
        err(f"--party-type is required and must be registered. Standard: {VALID_PARTY_TYPES}")
    party_id = args.party_id
    if not party_id:
        err("--party-id is required")
    amount = args.ple_amount
    if not amount:
        err("--amount is required")
    posting_date = args.posting_date
    if not posting_date:
        err("--posting-date is required")
    account_id = args.account_id
    if not account_id:
        err("--account-id is required")

    ple_id = str(uuid.uuid4())
    dec_amount = round_currency(to_decimal(amount))

    sql, _ = insert_row("payment_ledger_entry", {
        "id": P(), "posting_date": P(), "account_id": P(),
        "party_type": P(), "party_id": P(),
        "voucher_type": P(), "voucher_id": P(),
        "against_voucher_type": P(), "against_voucher_id": P(),
        "amount": P(), "amount_in_account_currency": P(), "currency": P(),
    })
    conn.execute(sql,
        (ple_id, posting_date, account_id, party_type, party_id,
         voucher_type, voucher_id,
         against_voucher_type, args.against_voucher_id,
         str(dec_amount), str(dec_amount), "USD"))

    audit(conn, "erpclaw-payments", "create-payment-ledger-entry", "payment_ledger_entry", ple_id,
           new_values={"voucher_type": voucher_type, "amount": str(dec_amount)})
    conn.commit()

    ok({"status": "created", "ple_id": ple_id})


# ---------------------------------------------------------------------------
# 9. get-outstanding
# ---------------------------------------------------------------------------

def get_outstanding(conn, args):
    """Get outstanding amounts for a party from payment ledger entries.

    Reads the party ledger through the CANONICAL rules
    (``erpclaw_lib.party_ledger``, ADR-0032 Decision 2 / F18) — reader
    disposition R2. Two things changed with Wave G F2 and both were defects:

    - LIVENESS. This used to filter a flat ``delinked = 0``, which drops a
      payment's delinked original while keeping its active cancel mirror, so
      every party read wrong after a ``cancel-payment``. Payment rows are now
      netted reversal-inclusive; document rows still require ``delinked = 0``.
    - ATTRIBUTION. This used to group on the row's OWN ``(voucher_type,
      voucher_id)``, which collided every per-allocation row into the paying
      payment's bucket instead of reducing the invoice it was applied to. Rows
      now bucket by their against-voucher when one is present and is not the
      payment itself, so a payment's allocations reduce the INVOICE buckets and
      the party-level + compensation rows stay in the payment's own bucket
      (correction C6 — there is no ``(None, None)`` bucket).

    F18: this is the PARTY-level truth (bound always-on by INV-27). The
    per-document truth is ``invoice.outstanding_amount`` (bound by INV-25), which
    is what ``check-overdue`` reads. The two are equal by INV-25 per document and
    therefore by summation per party; no reader may invent a third source.
    """
    party_type = args.party_type
    if not party_type:
        err("--party-type is required")
    party_id = args.party_id
    if not party_id:
        err("--party-id is required")

    ple = Table("payment_ledger_entry")
    bucket_type = party_ledger_rules.bucket_voucher_type_term()
    bucket_id = party_ledger_rules.bucket_voucher_id_term()
    base = (Q.from_(ple)
            .where(ple.party_type == P())
            .where(ple.party_id == P())
            .where(party_ledger_rules.live_rows_criterion()))
    params = [party_type, party_id]

    if args.voucher_type:
        # FINDING-006: filtering by "Sales Invoice" should match stored
        # "sales_invoice" PLE rows. The filter applies to the ATTRIBUTED bucket,
        # not the raw column, so "show me this invoice" returns the payments
        # applied to it as well as the invoice's own row.
        base = base.where(bucket_type == P())
        params.append(canonical_voucher_type(args.voucher_type))
    if args.voucher_id:
        base = base.where(bucket_id == P())
        params.append(args.voucher_id)

    # Aggregate outstanding by ATTRIBUTED voucher
    q = (base.select(
             bucket_type.as_("voucher_type"), bucket_id.as_("voucher_id"),
             DecimalSum(ple.amount).as_("outstanding_amount"),
             fn.Min(ple.posting_date).as_("posting_date"))
         .groupby(bucket_type, bucket_id)
         .having(LiteralValue('CAST(decimal_sum("amount") AS NUMERIC) != 0'))
         .orderby(LiteralValue('MIN("posting_date")')))
    rows = conn.execute(q.get_sql(), params).fetchall()

    vouchers = []
    total_outstanding = Decimal("0")
    for row in rows:
        outstanding = round_currency(to_decimal(str(row["outstanding_amount"])))
        total_outstanding += outstanding
        vouchers.append({
            "voucher_type": row["voucher_type"],
            "voucher_id": row["voucher_id"],
            "outstanding_amount": str(outstanding),
            "posting_date": row["posting_date"],
        })

    ok({"outstanding": str(round_currency(total_outstanding)),
         "vouchers": vouchers})


# ---------------------------------------------------------------------------
# 10. get-unallocated-payments
# ---------------------------------------------------------------------------

def get_unallocated_payments(conn, args):
    """Get payments with unallocated amounts for a party."""
    party_type = args.party_type
    if not party_type:
        err("--party-type is required")
    party_id = args.party_id
    if not party_id:
        err("--party-id is required")
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    q = (Q.from_(PE)
         .select(PE.id, PE.naming_series, PE.paid_amount,
                 PE.unallocated_amount, PE.posting_date)
         .where(PE.party_type == P())
         .where(PE.party_id == P())
         .where(PE.company_id == P())
         .where(PE.status == P())
         .where(LiteralValue('CAST("unallocated_amount" AS NUMERIC) > 0'))
         .orderby(PE.posting_date))
    rows = conn.execute(q.get_sql(), (party_type, party_id, company_id, "submitted")).fetchall()

    ok({"payments": [row_to_dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# 11. allocate-payment
# ---------------------------------------------------------------------------

def allocate_payment(conn, args):
    """Allocate a submitted payment to a voucher (invoice)."""
    pe_id = args.payment_entry_id
    if not pe_id:
        err("--payment-entry-id is required")
    voucher_type = args.voucher_type
    if not voucher_type:
        err("--voucher-type is required")
    # Canonicalize at the write boundary (gateway may pass "Sales Invoice").
    voucher_type = canonical_voucher_type(voucher_type)
    voucher_id = args.voucher_id
    if not voucher_id:
        err("--voucher-id is required")
    allocated_amount = args.allocated_amount
    if not allocated_amount:
        err("--allocated-amount is required")

    pe = _get_pe_or_err(conn, pe_id)
    if pe["status"] != "submitted":
        err(f"Cannot allocate: payment is '{pe['status']}' (must be 'submitted')")

    amount = round_currency(to_decimal(allocated_amount))
    unallocated = to_decimal(pe["unallocated_amount"])

    if amount <= 0:
        err("--allocated-amount must be > 0")
    if amount > unallocated:
        err(f"Allocated amount ({amount}) exceeds unallocated ({unallocated})")

    alloc_id = str(uuid.uuid4())
    alloc_sql, _ = insert_row("payment_allocation", {
        "id": P(), "payment_entry_id": P(), "voucher_type": P(),
        "voucher_id": P(), "allocated_amount": P(),
    })
    conn.execute(alloc_sql, (alloc_id, pe_id, voucher_type, voucher_id, str(amount)))

    _recalc_unallocated(conn, pe_id)

    # S2: if this payment's advance portion was routed to an advance sub-account at
    # submit time, applying it to an invoice must RECLASSIFY that amount out of the
    # advance account and into the AR/AP control account (new offsetting GL entries,
    # never editing the original posting — immutable GL).
    #   customer (receive): DR Advance-from-Customer / CR AR(paid_from)
    #   supplier (pay):      DR AP(paid_to) / CR Advance-to-Supplier
    adv = pe["advance_account_id"]
    if adv:
        if pe["payment_type"] == "receive":
            reclass = [
                {"account_id": adv, "debit": str(amount), "credit": "0",
                 "party_type": pe["party_type"], "party_id": pe["party_id"]},
                {"account_id": pe["paid_from_account"], "debit": "0", "credit": str(amount),
                 "party_type": pe["party_type"], "party_id": pe["party_id"]},
            ]
        else:  # pay
            reclass = [
                {"account_id": pe["paid_to_account"], "debit": str(amount), "credit": "0",
                 "party_type": pe["party_type"], "party_id": pe["party_id"]},
                {"account_id": adv, "debit": "0", "credit": str(amount),
                 "party_type": pe["party_type"], "party_id": pe["party_id"]},
            ]
        try:
            validate_gl_entries(conn, reclass, pe["company_id"], pe["posting_date"],
                                voucher_type="payment_entry")
            insert_gl_entries(conn, reclass, voucher_type="payment_entry", voucher_id=pe_id,
                              posting_date=pe["posting_date"], company_id=pe["company_id"],
                              remarks=f"Advance applied to {voucher_type} {voucher_id}",
                              entry_set=f"advance_alloc_{alloc_id}")
        except ValueError as e:
            conn.rollback()
            sys.stderr.write(f"[erpclaw-payments] {e}\n")
            err(f"Advance reclassification GL failed: {e}")

    # Clear the invoice this allocation targets: sync outstanding/status + post
    # the per-allocation PLE (INV-22). Only this one allocation is processed here.
    try:
        cleared = _clear_invoice_allocation(conn, pe, voucher_type, voucher_id, amount)
    except ValueError as e:
        conn.rollback()
        sys.stderr.write(f"[erpclaw-payments] {e}\n")
        err(f"Payment allocation failed: {e}")

    # Guard the false-success: an invoice-type voucher that cleared nothing is an
    # error (the doc must have been synced). Advance / on-account voucher types
    # legitimately clear nothing and return cleared=False without erroring.
    if voucher_type in INVOICE_VOUCHER_TYPES and not cleared:
        conn.rollback()
        sys.stderr.write(
            "[erpclaw-payments] allocation named an invoice but cleared no "
            "document\n")
        err("Allocation named an invoice but cleared no document")

    # Wave G F2 (M38), correction C3 — lifecycle site 2. The new allocation both
    # consumes residual and (for an invoice) writes its own per-allocation row,
    # so the party-level compensation moves by exactly this allocation.
    _post_party_residual_compensation(conn, pe_id)

    # Get updated unallocated
    qu = Q.from_(PE).select(PE.unallocated_amount).where(PE.id == P())
    updated = conn.execute(qu.get_sql(), (pe_id,)).fetchone()

    audit(conn, "erpclaw-payments", "allocate-payment", "payment_allocation", alloc_id,
           new_values={"payment_entry_id": pe_id, "voucher_id": voucher_id,
                       "allocated_amount": str(amount)})
    conn.commit()

    ok({"status": "created", "allocation_id": alloc_id,
         "document_cleared": bool(cleared),
         "remaining_unallocated": updated["unallocated_amount"]})


# ---------------------------------------------------------------------------
# 12. reconcile-payments
# ---------------------------------------------------------------------------

def reconcile_payments(conn, args):
    """Auto-reconcile payments against outstanding invoices (FIFO)."""
    party_type = args.party_type
    if not party_type:
        err("--party-type is required")
    party_id = args.party_id
    if not party_id:
        err("--party-id is required")
    company_id = args.company_id
    if not company_id:
        err("--company-id is required")

    # Get unallocated submitted payments (FIFO by posting_date)
    # Numeric compare on TEXT-stored amount via CAST (portable; SQLite + PG)
    payments = conn.execute(
        """SELECT id, paid_amount, unallocated_amount, posting_date
           FROM payment_entry
           WHERE party_type = ? AND party_id = ? AND company_id = ?
             AND status = 'submitted'
             AND CAST(unallocated_amount AS NUMERIC) > 0
           ORDER BY posting_date, created_at""",
        (party_type, party_id, company_id),
    ).fetchall()

    # Get outstanding vouchers from PLE (FIFO by posting_date)
    # Numeric compare on the decimal_sum aggregate via CAST (portable; SQLite + PG)
    outstanding_rows = conn.execute(
        """SELECT voucher_type, voucher_id,
               decimal_sum(amount) AS outstanding
           FROM payment_ledger_entry
           WHERE party_type = ? AND party_id = ? AND delinked = 0
             AND voucher_type IN ('sales_invoice', 'purchase_invoice')
           GROUP BY voucher_type, voucher_id
           HAVING CAST(decimal_sum(amount) AS NUMERIC) > 0
           ORDER BY MIN(posting_date)""",
        (party_type, party_id),
    ).fetchall()

    matched = []
    pay_idx = 0
    inv_idx = 0
    pay_list = [row_to_dict(p) for p in payments]
    inv_list = [row_to_dict(r) for r in outstanding_rows]
    pe_cache = {}  # payment_entry_id -> full pe row (for per-allocation PLE)

    # Track remaining amounts
    for p in pay_list:
        p["remaining"] = to_decimal(p["unallocated_amount"])
    for inv in inv_list:
        inv["remaining"] = to_decimal(str(inv["outstanding"]))

    while pay_idx < len(pay_list) and inv_idx < len(inv_list):
        pay = pay_list[pay_idx]
        inv = inv_list[inv_idx]

        if pay["remaining"] <= 0:
            pay_idx += 1
            continue
        if inv["remaining"] <= 0:
            inv_idx += 1
            continue

        alloc_amount = min(pay["remaining"], inv["remaining"])
        alloc_amount = round_currency(alloc_amount)

        # Create allocation
        alloc_id = str(uuid.uuid4())
        recon_sql, _ = insert_row("payment_allocation", {
            "id": P(), "payment_entry_id": P(), "voucher_type": P(),
            "voucher_id": P(), "allocated_amount": P(),
        })
        conn.execute(recon_sql,
            (alloc_id, pay["id"], inv["voucher_type"], inv["voucher_id"],
             str(alloc_amount)))

        # Clear the matched invoice: sync outstanding/status + per-allocation PLE
        # (INV-22). reconcile only matches sales_invoice/purchase_invoice (above),
        # so every match is a document allocation. Each match processed once.
        if pay["id"] not in pe_cache:
            pe_cache[pay["id"]] = _get_pe_or_err(conn, pay["id"])
        try:
            cleared = _clear_invoice_allocation(
                conn, pe_cache[pay["id"]], inv["voucher_type"],
                inv["voucher_id"], alloc_amount)
        except ValueError as e:
            conn.rollback()
            sys.stderr.write(f"[erpclaw-payments] {e}\n")
            err(f"Payment reconciliation failed: {e}")
        # reconcile only matches sales_invoice/purchase_invoice (the outstanding
        # query above filters to those), so every match MUST clear a document.
        # A False here means a voucher_type drifted from canonical — fail loud
        # rather than silently report a match that moved nothing.
        if not cleared:
            conn.rollback()
            sys.stderr.write(
                "[erpclaw-payments] reconcile matched an invoice but cleared "
                "no document\n")
            err("Reconcile matched an invoice but cleared no document")

        pay["remaining"] -= alloc_amount
        inv["remaining"] -= alloc_amount

        matched.append({
            "payment_id": pay["id"],
            "voucher_id": inv["voucher_id"],
            "allocated_amount": str(alloc_amount),
        })

        if pay["remaining"] <= 0:
            pay_idx += 1
        if inv["remaining"] <= 0:
            inv_idx += 1

    # Update unallocated amounts on all affected payments
    for pay in pay_list:
        _recalc_unallocated(conn, pay["id"])
        # Wave G F2 (M38), correction C3 — lifecycle site 3. Every payment the
        # FIFO walk touched changed its live-allocation set; a payment it did not
        # touch produces delta 0 and writes nothing, so the loop stays over the
        # full list rather than a hand-tracked subset.
        _post_party_residual_compensation(conn, pay["id"])

    unmatched_payments = sum(1 for p in pay_list if p["remaining"] > 0)
    unmatched_invoices = sum(1 for inv in inv_list if inv["remaining"] > 0)

    conn.commit()

    ok({"matched": matched,
         "unmatched_payments": unmatched_payments,
         "unmatched_invoices": unmatched_invoices})


# ---------------------------------------------------------------------------
# 13. bank-reconciliation
# ---------------------------------------------------------------------------

def bank_reconciliation(conn, args):
    """Read-only bank reconciliation: compare GL balance with expected."""
    bank_account_id = args.bank_account_id
    if not bank_account_id:
        err("--bank-account-id is required")
    from_date = args.from_date
    if not from_date:
        err("--from-date is required")
    to_date = args.to_date
    if not to_date:
        err("--to-date is required")

    # Verify account exists
    qa = Q.from_(ACCOUNT).select(ACCOUNT.id, ACCOUNT.name).where(ACCOUNT.id == P())
    acct = conn.execute(qa.get_sql(), (bank_account_id,)).fetchone()
    if not acct:
        err(f"Bank account {bank_account_id} not found")

    # Get GL entries for this bank account in date range
    qg = (Q.from_(GL)
          .select(fn.Count("*").as_("entry_count"),
                  fn.Coalesce(DecimalSum(GL.debit), ValueWrapper("0")).as_("total_debit"),
                  fn.Coalesce(DecimalSum(GL.credit), ValueWrapper("0")).as_("total_credit"))
          .where(GL.account_id == P())
          .where(GL.posting_date >= P())
          .where(GL.posting_date <= P())
          .where(GL.is_cancelled == P()))
    rows = conn.execute(qg.get_sql(), (bank_account_id, from_date, to_date, 0)).fetchone()

    gl_balance = round_currency(
        to_decimal(str(rows["total_debit"])) - to_decimal(str(rows["total_credit"]))
    )

    # Get payment entries hitting this bank account in date range
    pe = Table("payment_entry")
    qp = (Q.from_(pe).select(fn.Count("*"))
          .where((pe.paid_from_account == P()) | (pe.paid_to_account == P()))
          .where(pe.posting_date >= P())
          .where(pe.posting_date <= P())
          .where(pe.status == P()))
    payment_count = conn.execute(
        qp.get_sql(), (bank_account_id, bank_account_id, from_date, to_date, "submitted")
    ).fetchone()[0]

    ok({
        "bank_account": dict(acct)["name"],
        "from_date": from_date,
        "to_date": to_date,
        "gl_entries": rows["entry_count"],
        "gl_balance": str(gl_balance),
        "payment_entries": payment_count,
    })


# ---------------------------------------------------------------------------
# 14. status
# ---------------------------------------------------------------------------

def status(conn, args):
    """Show payment entry counts and totals."""
    company_id = resolve_company_id(conn,
                                    getattr(args, 'company_id', None),
                                    getattr(args, 'company_name', None))

    pe = Table("payment_entry")
    q1 = (Q.from_(pe)
          .select(pe.status, fn.Count("*").as_("cnt"),
                  fn.Coalesce(DecimalSum(pe.paid_amount), ValueWrapper("0")).as_("total"))
          .where(pe.company_id == P())
          .groupby(pe.status))
    rows = conn.execute(q1.get_sql(), (company_id,)).fetchall()

    counts = {"total": 0, "draft": 0, "submitted": 0, "cancelled": 0}
    total_received = Decimal("0")
    total_paid = Decimal("0")
    for row in rows:
        counts[row["status"]] = row["cnt"]
        counts["total"] += row["cnt"]

    # Get totals by payment type for submitted only
    q2 = (Q.from_(pe)
          .select(pe.payment_type,
                  fn.Coalesce(DecimalSum(pe.paid_amount), ValueWrapper("0")).as_("total"))
          .where(pe.company_id == P())
          .where(pe.status == P())
          .groupby(pe.payment_type))
    type_rows = conn.execute(q2.get_sql(), (company_id, "submitted")).fetchall()
    for row in type_rows:
        if row["payment_type"] == "receive":
            total_received = round_currency(to_decimal(str(row["total"])))
        elif row["payment_type"] == "pay":
            total_paid = round_currency(to_decimal(str(row["total"])))

    counts["total_received"] = str(total_received)
    counts["total_paid"] = str(total_paid)

    ok(counts)


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

ACTIONS = {
    "add-payment": add_payment,
    "update-payment": update_payment,
    "get-payment": get_payment,
    "list-payments": list_payments,
    "submit-payment": submit_payment,
    "cancel-payment": cancel_payment,
    "delete-payment": delete_payment,
    "create-payment-ledger-entry": create_payment_ledger_entry,
    # Wave G F17a. Deliberately NOT part of the payment lifecycle above: a
    # write-off moves no cash, so it creates no payment_entry.
    "write-off-invoice": write_off_invoice,
    "get-outstanding": get_outstanding,
    "get-unallocated-payments": get_unallocated_payments,
    "allocate-payment": allocate_payment,
    # S2: SAP Business One vocabulary aliases for the existing advance lifecycle (same semantics)
    "list-open-advances": get_unallocated_payments,
    "apply-advance-to-invoice": allocate_payment,
    "reconcile-payments": reconcile_payments,
    "bank-reconciliation": bank_reconciliation,
    "status": status,
}


def main():
    parser = SafeArgumentParser(description="ERPClaw Payments Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Payment entry fields
    parser.add_argument("--payment-entry-id")
    parser.add_argument("--company-id")
    parser.add_argument("--company", dest="company_name", default=None)  # NL: company by name
    parser.add_argument("--payment-type")
    parser.add_argument("--posting-date")
    parser.add_argument("--party-type")
    parser.add_argument("--party-id")
    parser.add_argument("--paid-from-account")
    parser.add_argument("--paid-to-account")
    parser.add_argument("--paid-amount")
    parser.add_argument("--payment-currency", default="USD")
    parser.add_argument("--exchange-rate", default="1")
    parser.add_argument("--reference-number")
    parser.add_argument("--reference-date")
    parser.add_argument("--allocations")
    # WS2 D3: JSON array of {account_id, amount, type, description?};
    # type ∈ tds|commission|early_payment_discount|write_off|other
    # (write_off = Wave G F17b, the residual taken at payment time; the no-cash
    #  standalone write-off is write-off-invoice below, not a deduction)
    parser.add_argument("--deductions")

    # Allocation
    parser.add_argument("--voucher-type")
    parser.add_argument("--voucher-id")
    parser.add_argument("--allocated-amount")

    # PLE
    parser.add_argument("--amount", dest="ple_amount")
    parser.add_argument("--account-id")
    parser.add_argument("--against-voucher-type")
    parser.add_argument("--against-voucher-id")

    # Write-off (F17a). Its own flags rather than reusing --amount/--account-id:
    # those already carry PLE meanings on this parser, and an accounting action
    # that silently shares a flag with a raw-ledger primitive is how a write-off
    # ends up posted against the wrong account.
    parser.add_argument("--write-off-amount")
    parser.add_argument("--write-off-account-id")
    parser.add_argument("--reason")
    parser.add_argument("--cost-center-id")

    # Bank reconciliation
    parser.add_argument("--bank-account-id")

    # List filters
    parser.add_argument("--status", dest="pe_status")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")

    args, unknown = parser.parse_known_args()
    check_unknown_args(parser, unknown)
    check_input_lengths(args)
    action_fn = ACTIONS[args.action]

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
        action_fn(conn, args)
    except Exception as e:
        conn.rollback()
        sys.stderr.write(f"[erpclaw-payments] {e}\n")
        err("An unexpected error occurred")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
