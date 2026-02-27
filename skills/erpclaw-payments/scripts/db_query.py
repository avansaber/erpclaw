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
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
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
    from erpclaw_lib.naming import get_next_name
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
    from erpclaw_lib.query_helpers import resolve_company_id
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw-setup first: clawhub install erpclaw-setup", "suggestion": "clawhub install erpclaw-setup"}))
    sys.exit(1)

REQUIRED_TABLES = ["company", "account"]

VALID_PAYMENT_TYPES = ("receive", "pay", "internal_transfer")
VALID_PARTY_TYPES = ("customer", "supplier", "employee")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_pe_or_err(conn, payment_entry_id: str) -> dict:
    """Fetch a payment entry by ID. Calls err() if not found."""
    row = conn.execute(
        "SELECT * FROM payment_entry WHERE id = ?", (payment_entry_id,)
    ).fetchone()
    if not row:
        err(f"Payment entry {payment_entry_id} not found",
             suggestion="Use 'list payments' to see available payment entries.")
    return row_to_dict(row)


def _get_allocations(conn, payment_entry_id: str) -> list[dict]:
    """Fetch allocations for a payment entry."""
    rows = conn.execute(
        "SELECT * FROM payment_allocation WHERE payment_entry_id = ? ORDER BY rowid",
        (payment_entry_id,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def _insert_allocations(conn, payment_entry_id: str, allocations: list[dict]):
    """Insert payment allocation rows and return total allocated."""
    total_allocated = Decimal("0")
    for alloc in allocations:
        alloc_id = str(uuid.uuid4())
        amount = round_currency(to_decimal(alloc.get("allocated_amount", "0")))
        total_allocated += amount
        conn.execute(
            """INSERT INTO payment_allocation
               (id, payment_entry_id, voucher_type, voucher_id, allocated_amount)
               VALUES (?, ?, ?, ?, ?)""",
            (alloc_id, payment_entry_id,
             alloc["voucher_type"], alloc["voucher_id"], str(amount)),
        )
    return total_allocated


def _recalc_unallocated(conn, payment_entry_id: str):
    """Recalculate and update unallocated_amount on a payment entry."""
    pe = conn.execute(
        "SELECT paid_amount FROM payment_entry WHERE id = ?", (payment_entry_id,)
    ).fetchone()
    if not pe:
        return
    paid = to_decimal(pe["paid_amount"])
    row = conn.execute(
        """SELECT COALESCE(decimal_sum(allocated_amount), '0') AS total
           FROM payment_allocation WHERE payment_entry_id = ?""",
        (payment_entry_id,),
    ).fetchone()
    allocated = to_decimal(str(row["total"]))
    unallocated = round_currency(paid - allocated)
    conn.execute(
        "UPDATE payment_entry SET unallocated_amount = ?, updated_at = datetime('now') WHERE id = ?",
        (str(unallocated), payment_entry_id),
    )


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
        if not party_type or party_type not in VALID_PARTY_TYPES:
            err(f"--party-type is required. Valid: {VALID_PARTY_TYPES}")
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
    if not conn.execute("SELECT id FROM company WHERE id = ?", (company_id,)).fetchone():
        err(f"Company {company_id} not found")

    # Validate accounts exist
    for acct_id, label in [(paid_from, "paid-from-account"), (paid_to, "paid-to-account")]:
        if not conn.execute("SELECT id FROM account WHERE id = ?", (acct_id,)).fetchone():
            err(f"Account {acct_id} ({label}) not found")

    amount = round_currency(to_decimal(paid_amount))
    if amount <= 0:
        err("--paid-amount must be > 0")

    exchange_rate = to_decimal(args.exchange_rate or "1")
    received_amount = round_currency(amount * exchange_rate)
    payment_currency = args.payment_currency or "USD"

    pe_id = str(uuid.uuid4())
    naming = get_next_name(conn, "payment_entry", company_id=company_id)

    conn.execute(
        """INSERT INTO payment_entry
           (id, naming_series, payment_type, posting_date, party_type, party_id,
            paid_from_account, paid_to_account, paid_amount, received_amount,
            payment_currency, exchange_rate, reference_number, reference_date,
            status, unallocated_amount, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (pe_id, naming, payment_type, posting_date,
         party_type, party_id, paid_from, paid_to,
         str(amount), str(received_amount),
         payment_currency, str(exchange_rate),
         args.reference_number, args.reference_date,
         str(amount),  # unallocated = full amount initially
         company_id),
    )

    # Insert allocations if provided
    if args.allocations:
        try:
            allocs = json.loads(args.allocations) if isinstance(args.allocations, str) else args.allocations
        except json.JSONDecodeError as e:
            err("Invalid JSON format in --allocations")
        _insert_allocations(conn, pe_id, allocs)
        _recalc_unallocated(conn, pe_id)

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
    """Update a draft payment entry."""
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
        conn.execute(
            """UPDATE payment_entry SET paid_amount = ?, received_amount = ?,
               updated_at = datetime('now') WHERE id = ?""",
            (str(amount), str(received), pe_id),
        )
        updated_fields.append("paid_amount")

    if args.reference_number is not None:
        old_values["reference_number"] = pe["reference_number"]
        conn.execute(
            "UPDATE payment_entry SET reference_number = ?, updated_at = datetime('now') WHERE id = ?",
            (args.reference_number, pe_id),
        )
        updated_fields.append("reference_number")

    if args.allocations:
        try:
            allocs = json.loads(args.allocations) if isinstance(args.allocations, str) else args.allocations
        except json.JSONDecodeError as e:
            err("Invalid JSON format in --allocations")
        conn.execute("DELETE FROM payment_allocation WHERE payment_entry_id = ?", (pe_id,))
        _insert_allocations(conn, pe_id, allocs)
        _recalc_unallocated(conn, pe_id)
        updated_fields.append("allocations")

    if not updated_fields:
        err("No fields to update")

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

    formatted_allocs = [{
        "id": a["id"],
        "voucher_type": a["voucher_type"],
        "voucher_id": a["voucher_id"],
        "allocated_amount": a["allocated_amount"],
        "exchange_gain_loss": a.get("exchange_gain_loss", "0"),
    } for a in allocs]

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
    })


# ---------------------------------------------------------------------------
# 4. list-payments
# ---------------------------------------------------------------------------

def list_payments(conn, args):
    """List payment entries with filtering."""
    company_id = resolve_company_id(conn, getattr(args, 'company_id', None))

    conditions = ["pe.company_id = ?"]
    params = [company_id]

    if args.payment_type:
        conditions.append("pe.payment_type = ?")
        params.append(args.payment_type)
    if args.party_type:
        conditions.append("pe.party_type = ?")
        params.append(args.party_type)
    if args.party_id:
        conditions.append("pe.party_id = ?")
        params.append(args.party_id)
    if args.pe_status:
        conditions.append("pe.status = ?")
        params.append(args.pe_status)
    if args.from_date:
        conditions.append("pe.posting_date >= ?")
        params.append(args.from_date)
    if args.to_date:
        conditions.append("pe.posting_date <= ?")
        params.append(args.to_date)

    where = " AND ".join(conditions)

    count_row = conn.execute(
        f"SELECT COUNT(*) FROM payment_entry pe WHERE {where}", params
    ).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    params.extend([limit, offset])

    rows = conn.execute(
        f"""SELECT pe.id, pe.naming_series, pe.payment_type, pe.posting_date,
               pe.party_type, pe.party_id, pe.paid_amount, pe.status,
               pe.unallocated_amount
           FROM payment_entry pe
           WHERE {where}
           ORDER BY pe.posting_date DESC, pe.created_at DESC
           LIMIT ? OFFSET ?""",
        params,
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
            inv = conn.execute(
                "SELECT posting_date, payment_terms_id FROM sales_invoice WHERE id = ?",
                (vid,),
            ).fetchone()
        else:
            inv = conn.execute(
                "SELECT posting_date, payment_terms_id FROM purchase_invoice WHERE id = ?",
                (vid,),
            ).fetchone()
        if not inv or not inv["payment_terms_id"]:
            continue

        pt = conn.execute(
            "SELECT discount_percentage, discount_days FROM payment_terms WHERE id = ?",
            (inv["payment_terms_id"],),
        ).fetchone()
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
        acct = conn.execute(
            "SELECT id FROM account WHERE name = ? AND company_id = ?",
            (disc_name, pe["company_id"]),
        ).fetchone()
        if acct:
            discount_account_id = acct["id"]
        # Get default cost center for P&L tracking
        cc = conn.execute(
            "SELECT id FROM cost_center WHERE company_id = ? AND is_group = 0 LIMIT 1",
            (pe["company_id"],),
        ).fetchone()
        if cc:
            cost_center_id = cc["id"]

    return total_discount, discount_account_id, details, cost_center_id


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

    paid_amount = to_decimal(pe["paid_amount"])
    allocations = _get_allocations(conn, pe_id)

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

    # Apply multi-currency: set currency/exchange_rate on GL entries
    payment_currency = pe["payment_currency"] or "USD"
    payment_rate = to_decimal(pe["exchange_rate"] or "1")
    if payment_currency != "USD" or payment_rate != Decimal("1"):
        prepare_multicurrency_entries(gl_entries, payment_currency, payment_rate)

    # Compute FX gain/loss on allocated invoices
    fx_gain_loss_total = Decimal("0")
    if allocations and payment_rate != Decimal("1"):
        company = conn.execute(
            "SELECT exchange_gain_loss_account_id FROM company WHERE id = ?",
            (pe["company_id"],),
        ).fetchone()
        fx_account_id = company["exchange_gain_loss_account_id"] if company else None

        for alloc in allocations:
            inv_rate = Decimal("1")
            # Try to get original invoice exchange rate
            if alloc.get("reference_type") == "sales_invoice":
                inv_row = conn.execute(
                    "SELECT exchange_rate FROM sales_invoice WHERE id = ?",
                    (alloc["reference_id"],),
                ).fetchone()
                if inv_row and inv_row["exchange_rate"]:
                    inv_rate = to_decimal(inv_row["exchange_rate"])
            elif alloc.get("reference_type") == "purchase_invoice":
                inv_row = conn.execute(
                    "SELECT exchange_rate FROM purchase_invoice WHERE id = ?",
                    (alloc["reference_id"],),
                ).fetchone()
                if inv_row and inv_row["exchange_rate"]:
                    inv_rate = to_decimal(inv_row["exchange_rate"])

            if inv_rate != payment_rate:
                alloc_amount = to_decimal(alloc["allocated_amount"])
                gl = calculate_exchange_gain_loss(
                    alloc_amount, payment_rate, inv_rate
                )
                fx_gain_loss_total += gl
                # Update allocation record
                conn.execute(
                    "UPDATE payment_allocation SET exchange_gain_loss = ? WHERE id = ?",
                    (str(gl), alloc["id"]),
                )

        # Post FX gain/loss GL entries if there's a net amount
        if fx_gain_loss_total != 0 and fx_account_id:
            post_exchange_gain_loss(
                gl_entries, fx_gain_loss_total, fx_account_id
            )
            # FX entry needs a cost center for P&L tracking
            if gl_entries[-1].get("account_id") == fx_account_id:
                # Use the first cost center found, or look up default
                default_cc = conn.execute(
                    "SELECT default_cost_center_id FROM company WHERE id = ?",
                    (pe["company_id"],),
                ).fetchone()
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
        conn.execute(
            """INSERT INTO payment_ledger_entry
               (id, posting_date, account_id, party_type, party_id,
                voucher_type, voucher_id, amount, amount_in_account_currency,
                currency, remarks)
               VALUES (?, ?, ?, ?, ?, 'payment_entry', ?, ?, ?, ?, ?)""",
            (ple_id, pe["posting_date"], ple_account,
             pe["party_type"], pe["party_id"],
             pe_id, ple_amount, ple_amount,
             pe["payment_currency"],
             f"Payment {pe['naming_series']}"),
        )

    conn.execute(
        """UPDATE payment_entry SET status = 'submitted',
           updated_at = datetime('now') WHERE id = ?""",
        (pe_id,),
    )

    result = {"status": "submitted", "payment_entry_id": pe_id,
              "gl_entries_created": len(gl_ids), "outstanding_updated": True}
    if discount_amount > 0:
        result["early_payment_discount"] = {
            "discount_amount": str(discount_amount),
            "bank_amount": str(bank_amount),
            "details": discount_details,
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

    # Reverse PLE: mark existing as delinked, create offsetting entry
    ple_rows = conn.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type = 'payment_entry' AND voucher_id = ? AND delinked = 0""",
        (pe_id,),
    ).fetchall()
    for ple in ple_rows:
        ple_dict = row_to_dict(ple)
        conn.execute(
            "UPDATE payment_ledger_entry SET delinked = 1, updated_at = datetime('now') WHERE id = ?",
            (ple_dict["id"],),
        )
        # Create reversing PLE
        reversal_amount = str(round_currency(-to_decimal(ple_dict["amount"])))
        conn.execute(
            """INSERT INTO payment_ledger_entry
               (id, posting_date, account_id, party_type, party_id,
                voucher_type, voucher_id, amount, amount_in_account_currency,
                currency, remarks)
               VALUES (?, ?, ?, ?, ?, 'payment_entry', ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), pe["posting_date"], ple_dict["account_id"],
             ple_dict["party_type"], ple_dict["party_id"],
             pe_id, reversal_amount, reversal_amount,
             ple_dict["currency"],
             f"Reversal: Payment {pe['naming_series']}"),
        )

    conn.execute(
        """UPDATE payment_entry SET status = 'cancelled',
           updated_at = datetime('now') WHERE id = ?""",
        (pe_id,),
    )

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
    conn.execute("DELETE FROM payment_allocation WHERE payment_entry_id = ?", (pe_id,))
    conn.execute("DELETE FROM payment_deduction WHERE payment_entry_id = ?", (pe_id,))
    conn.execute("DELETE FROM payment_entry WHERE id = ?", (pe_id,))

    audit(conn, "erpclaw-payments", "delete-payment", "payment_entry", pe_id,
           old_values={"naming_series": naming})
    conn.commit()

    ok({"status": "deleted", "deleted": True})


# ---------------------------------------------------------------------------
# 8. create-payment-ledger-entry
# ---------------------------------------------------------------------------

def create_payment_ledger_entry(conn, args):
    """Create a PLE record. Called cross-skill by selling/buying on invoice submit."""
    voucher_type = args.voucher_type
    if not voucher_type:
        err("--voucher-type is required")
    voucher_id = args.voucher_id
    if not voucher_id:
        err("--voucher-id is required")
    party_type = args.party_type
    if not party_type or party_type not in VALID_PARTY_TYPES:
        err(f"--party-type is required. Valid: {VALID_PARTY_TYPES}")
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

    conn.execute(
        """INSERT INTO payment_ledger_entry
           (id, posting_date, account_id, party_type, party_id,
            voucher_type, voucher_id, against_voucher_type, against_voucher_id,
            amount, amount_in_account_currency, currency)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD')""",
        (ple_id, posting_date, account_id, party_type, party_id,
         voucher_type, voucher_id,
         args.against_voucher_type, args.against_voucher_id,
         str(dec_amount), str(dec_amount)),
    )

    audit(conn, "erpclaw-payments", "create-payment-ledger-entry", "payment_ledger_entry", ple_id,
           new_values={"voucher_type": voucher_type, "amount": str(dec_amount)})
    conn.commit()

    ok({"status": "created", "ple_id": ple_id})


# ---------------------------------------------------------------------------
# 9. get-outstanding
# ---------------------------------------------------------------------------

def get_outstanding(conn, args):
    """Get outstanding amounts for a party from payment ledger entries."""
    party_type = args.party_type
    if not party_type:
        err("--party-type is required")
    party_id = args.party_id
    if not party_id:
        err("--party-id is required")

    conditions = ["ple.party_type = ?", "ple.party_id = ?", "ple.delinked = 0"]
    params = [party_type, party_id]

    if args.voucher_type:
        conditions.append("ple.voucher_type = ?")
        params.append(args.voucher_type)
    if args.voucher_id:
        conditions.append("ple.voucher_id = ?")
        params.append(args.voucher_id)

    where = " AND ".join(conditions)

    # Aggregate outstanding by voucher
    rows = conn.execute(
        f"""SELECT ple.voucher_type, ple.voucher_id,
               decimal_sum(ple.amount) AS outstanding_amount,
               MIN(ple.posting_date) AS posting_date
           FROM payment_ledger_entry ple
           WHERE {where}
           GROUP BY ple.voucher_type, ple.voucher_id
           HAVING decimal_sum(ple.amount) + 0 != 0
           ORDER BY ple.posting_date""",
        params,
    ).fetchall()

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
    company_id = resolve_company_id(conn, getattr(args, 'company_id', None))

    rows = conn.execute(
        """SELECT id, naming_series, paid_amount, unallocated_amount, posting_date
           FROM payment_entry
           WHERE party_type = ? AND party_id = ? AND company_id = ?
             AND status = 'submitted'
             AND unallocated_amount + 0 > 0
           ORDER BY posting_date""",
        (party_type, party_id, company_id),
    ).fetchall()

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
    conn.execute(
        """INSERT INTO payment_allocation
           (id, payment_entry_id, voucher_type, voucher_id, allocated_amount)
           VALUES (?, ?, ?, ?, ?)""",
        (alloc_id, pe_id, voucher_type, voucher_id, str(amount)),
    )

    _recalc_unallocated(conn, pe_id)

    # Get updated unallocated
    updated = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?", (pe_id,)
    ).fetchone()

    audit(conn, "erpclaw-payments", "allocate-payment", "payment_allocation", alloc_id,
           new_values={"payment_entry_id": pe_id, "voucher_id": voucher_id,
                       "allocated_amount": str(amount)})
    conn.commit()

    ok({"status": "created", "allocation_id": alloc_id,
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
    payments = conn.execute(
        """SELECT id, paid_amount, unallocated_amount, posting_date
           FROM payment_entry
           WHERE party_type = ? AND party_id = ? AND company_id = ?
             AND status = 'submitted'
             AND unallocated_amount + 0 > 0
           ORDER BY posting_date, created_at""",
        (party_type, party_id, company_id),
    ).fetchall()

    # Get outstanding vouchers from PLE (FIFO by posting_date)
    outstanding_rows = conn.execute(
        """SELECT voucher_type, voucher_id,
               decimal_sum(amount) AS outstanding
           FROM payment_ledger_entry
           WHERE party_type = ? AND party_id = ? AND delinked = 0
             AND voucher_type IN ('sales_invoice', 'purchase_invoice')
           GROUP BY voucher_type, voucher_id
           HAVING decimal_sum(amount) + 0 > 0
           ORDER BY MIN(posting_date)""",
        (party_type, party_id),
    ).fetchall()

    matched = []
    pay_idx = 0
    inv_idx = 0
    pay_list = [row_to_dict(p) for p in payments]
    inv_list = [row_to_dict(r) for r in outstanding_rows]

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
        conn.execute(
            """INSERT INTO payment_allocation
               (id, payment_entry_id, voucher_type, voucher_id, allocated_amount)
               VALUES (?, ?, ?, ?, ?)""",
            (alloc_id, pay["id"], inv["voucher_type"], inv["voucher_id"],
             str(alloc_amount)),
        )

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
    acct = conn.execute("SELECT id, name FROM account WHERE id = ?",
                        (bank_account_id,)).fetchone()
    if not acct:
        err(f"Bank account {bank_account_id} not found")

    # Get GL entries for this bank account in date range
    rows = conn.execute(
        """SELECT COUNT(*) AS entry_count,
               COALESCE(decimal_sum(debit), '0') AS total_debit,
               COALESCE(decimal_sum(credit), '0') AS total_credit
           FROM gl_entry
           WHERE account_id = ? AND posting_date >= ? AND posting_date <= ?
             AND is_cancelled = 0""",
        (bank_account_id, from_date, to_date),
    ).fetchone()

    gl_balance = round_currency(
        to_decimal(str(rows["total_debit"])) - to_decimal(str(rows["total_credit"]))
    )

    # Get payment entries hitting this bank account in date range
    payment_count = conn.execute(
        """SELECT COUNT(*) FROM payment_entry
           WHERE (paid_from_account = ? OR paid_to_account = ?)
             AND posting_date >= ? AND posting_date <= ?
             AND status = 'submitted'""",
        (bank_account_id, bank_account_id, from_date, to_date),
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
    company_id = resolve_company_id(conn, getattr(args, 'company_id', None))

    rows = conn.execute(
        """SELECT status, COUNT(*) AS cnt,
               COALESCE(decimal_sum(paid_amount), '0') AS total
           FROM payment_entry
           WHERE company_id = ? GROUP BY status""",
        (company_id,),
    ).fetchall()

    counts = {"total": 0, "draft": 0, "submitted": 0, "cancelled": 0}
    total_received = Decimal("0")
    total_paid = Decimal("0")
    for row in rows:
        counts[row["status"]] = row["cnt"]
        counts["total"] += row["cnt"]

    # Get totals by payment type for submitted only
    type_rows = conn.execute(
        """SELECT payment_type,
               COALESCE(decimal_sum(paid_amount), '0') AS total
           FROM payment_entry
           WHERE company_id = ? AND status = 'submitted'
           GROUP BY payment_type""",
        (company_id,),
    ).fetchall()
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
    "get-outstanding": get_outstanding,
    "get-unallocated-payments": get_unallocated_payments,
    "allocate-payment": allocate_payment,
    "reconcile-payments": reconcile_payments,
    "bank-reconciliation": bank_reconciliation,
    "status": status,
}


def main():
    parser = argparse.ArgumentParser(description="ERPClaw Payments Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Payment entry fields
    parser.add_argument("--payment-entry-id")
    parser.add_argument("--company-id")
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

    # Allocation
    parser.add_argument("--voucher-type")
    parser.add_argument("--voucher-id")
    parser.add_argument("--allocated-amount")

    # PLE
    parser.add_argument("--amount", dest="ple_amount")
    parser.add_argument("--account-id")
    parser.add_argument("--against-voucher-type")
    parser.add_argument("--against-voucher-id")

    # Bank reconciliation
    parser.add_argument("--bank-account-id")

    # List filters
    parser.add_argument("--status", dest="pe_status")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")

    args, _unknown = parser.parse_known_args()
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
