"""Document payment-clearing engine (AR/AP outstanding + status sync).

Neutral transactional layer — same model as gl_posting.py / stock_posting.py.
Both the payments paths (submit-payment / allocate-payment / reconcile-payments)
and the selling/buying ``update-invoice-outstanding`` actions delegate the
compute-and-write of a document's ``outstanding_amount`` + ``status`` to the
functions here, so there is exactly ONE canonical implementation of the
clearing rule (no drift between modules).

Key functions:
- apply_payment_to_document():   reduce outstanding, flip to paid/partially_paid
- reverse_payment_on_document(): add outstanding back, restore submitted/partially_paid

NEVER commit inside these functions — the caller owns the transaction (mirrors
gl_posting.py). Money is Decimal throughout, stored TEXT. No floats.

Ownership note: writing to ``sales_invoice`` / ``purchase_invoice`` from this
neutral lib is exactly like gl_posting.insert_gl_entries writing ``gl_entry`` on
behalf of every module. The owning modules (selling/buying) also delegate here;
payments invokes the same shared write path rather than hand-rolling an UPDATE.
"""
from decimal import Decimal

from erpclaw_lib.decimal_utils import to_decimal, round_currency
from erpclaw_lib.query import Q, P, Table, Field, now
# canonical_voucher_type lives in the neutral voucher_types lib (FINDING-006) so
# every module can normalize doctype voucher_types without depending on this
# payments-specific module. Re-exported here so the FINDING-005 callers that
# import it from payment_clearing keep working unchanged.
from erpclaw_lib.voucher_types import canonical_voucher_type

# Documents that carry an outstanding_amount/status pair we sync. Any other
# voucher type (advance / on-account) is a no-op — it never clears a document.
_CLEARABLE_DOCS = {"sales_invoice", "purchase_invoice"}

# A document must be in one of these states to accept a payment application.
# Mirrors the selling/buying guard. A 'draft' has no GL yet; a 'paid' or
# 'cancelled' doc must not be re-cleared.
_CLEARABLE_STATUSES = ("submitted", "overdue", "partially_paid")


def _read_doc(conn, voucher_type, voucher_id, columns):
    """SELECT a document row via PyPika (dialect-portable, no f-string SQL).

    ``voucher_type`` is always a whitelisted constant from _CLEARABLE_DOCS, never
    user input — the Table() name is a fixed token, all values are bound params.
    """
    t = Table(voucher_type)
    q = Q.from_(t).select(*[Field(c) for c in columns]).where(Field("id") == P())
    return conn.execute(q.get_sql(), (voucher_id,)).fetchone()


def _write_doc(conn, voucher_type, voucher_id, outstanding_str, status):
    """UPDATE a document's outstanding/status/updated_at via PyPika (no f-string)."""
    t = Table(voucher_type)
    q = (Q.update(t)
         .set(Field("outstanding_amount"), P())
         .set(Field("status"), P())
         .set(Field("updated_at"), now())
         .where(Field("id") == P()))
    conn.execute(q.get_sql(), (outstanding_str, status, voucher_id))


def apply_payment_to_document(conn, voucher_type, voucher_id, allocated_amount):
    """Reduce a document's outstanding by ``allocated_amount`` and sync status.

    Runs inside the caller's open transaction — does NOT commit.

    Args:
        conn: open DB connection (caller owns the transaction).
        voucher_type: 'sales_invoice' | 'purchase_invoice'. Any other value is a
            no-op (advances/on-account never sync a document).
        voucher_id: the document id.
        allocated_amount: amount applied (str/int/Decimal; never float).

    Returns:
        dict {"voucher_type", "voucher_id", "outstanding_amount", "status",
        "applied": bool}. ``applied`` is False for the no-op path.

    Raises:
        ValueError: document not found, non-clearable status, non-positive
            amount, or over-application (amount > current outstanding — REJECT,
            never silently clamp; over-applying is a real data error).
    """
    if voucher_type not in _CLEARABLE_DOCS:
        return {"voucher_type": voucher_type, "voucher_id": voucher_id,
                "outstanding_amount": None, "status": None, "applied": False}

    row = _read_doc(conn, voucher_type, voucher_id, ("outstanding_amount", "status"))
    if row is None:
        raise ValueError(f"{voucher_type} {voucher_id} not found")

    status = row["status"]
    if status not in _CLEARABLE_STATUSES:
        raise ValueError(f"Cannot apply payment: {voucher_type} is '{status}'")

    amt = round_currency(to_decimal(allocated_amount))
    if amt <= 0:
        raise ValueError("allocated_amount must be > 0")

    current = to_decimal(row["outstanding_amount"])
    if amt > current:
        raise ValueError(
            f"Payment amount {amt} exceeds outstanding {current} "
            f"on {voucher_type} {voucher_id}"
        )

    new_outstanding = round_currency(current - amt)
    if new_outstanding == Decimal("0"):
        # Canonical zero is the bare "0" (matches selling's historical form and
        # INV-22's `outstanding_amount = '0'` paid-doc predicate), not "0.00".
        new_status = "paid"
        new_outstanding_str = "0"
    else:
        new_status = "partially_paid"
        new_outstanding_str = str(new_outstanding)

    _write_doc(conn, voucher_type, voucher_id, new_outstanding_str, new_status)

    return {"voucher_type": voucher_type, "voucher_id": voucher_id,
            "outstanding_amount": new_outstanding_str, "status": new_status,
            "applied": True}


def reverse_payment_on_document(conn, voucher_type, voucher_id,
                                allocated_amount, grand_total):
    """Add ``allocated_amount`` back to a document's outstanding (cancel path).

    Runs inside the caller's open transaction — does NOT commit.

    Status restoration rule:
    - If the restored outstanding equals ``grand_total`` (the document is fully
      un-paid again), status → 'submitted'.
    - Otherwise the document is still partially paid (by OTHER payments), so
      status → 'partially_paid'. Never flip a doc cleared by another payment
      back to 'submitted', and never go to 'paid' on a reversal.

    Args:
        conn: open DB connection (caller owns the transaction).
        voucher_type: 'sales_invoice' | 'purchase_invoice'. Any other value is a
            no-op.
        voucher_id: the document id.
        allocated_amount: amount to add back (str/int/Decimal; never float).
        grand_total: the document's grand_total, passed by the caller so this
            helper stays table-shape-agnostic.

    Returns:
        dict {"voucher_type", "voucher_id", "outstanding_amount", "status",
        "applied": bool}.

    Raises:
        ValueError: document not found, or non-positive amount.
    """
    if voucher_type not in _CLEARABLE_DOCS:
        return {"voucher_type": voucher_type, "voucher_id": voucher_id,
                "outstanding_amount": None, "status": None, "applied": False}

    row = _read_doc(conn, voucher_type, voucher_id, ("outstanding_amount",))
    if row is None:
        raise ValueError(f"{voucher_type} {voucher_id} not found")

    amt = round_currency(to_decimal(allocated_amount))
    if amt <= 0:
        raise ValueError("allocated_amount must be > 0")

    current = to_decimal(row["outstanding_amount"])
    restored = round_currency(current + amt)
    new_status = "submitted" if restored == round_currency(to_decimal(grand_total)) \
        else "partially_paid"

    _write_doc(conn, voucher_type, voucher_id, str(restored), new_status)

    return {"voucher_type": voucher_type, "voucher_id": voucher_id,
            "outstanding_amount": str(restored), "status": new_status,
            "applied": True}
