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
- release_allocations_on_document(): void the allocations a document cancel kills
- recalc_unallocated():          canonical payment residual (paid − live alloc − ded)
- post_party_residual_compensation(): the Fork-A party-level residual row (M38)

NEVER commit inside these functions — the caller owns the transaction (mirrors
gl_posting.py). Money is Decimal throughout, stored TEXT. No floats.

Ownership note: writing to ``sales_invoice`` / ``purchase_invoice`` from this
neutral lib is exactly like gl_posting.insert_gl_entries writing ``gl_entry`` on
behalf of every module. The owning modules (selling/buying) also delegate here;
payments invokes the same shared write path rather than hand-rolling an UPDATE.
The same rule covers the payments-owned tables written by
release_allocations_on_document(): the cancel paths delegate here instead of
reaching into payment_allocation / payment_ledger_entry themselves.
"""
import uuid
from decimal import Decimal

from erpclaw_lib.decimal_utils import to_decimal, round_currency
from erpclaw_lib.party_ledger import COMPENSATION_ROW_SQL, LIVE_ROW_SQL
from erpclaw_lib.query import Q, P, Table, Field, insert_row, update_row, now
from erpclaw_lib.vendor.pypika.terms import LiteralValue
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

# Only a SUBMITTED payment's allocations may be released (Wave G F1, correction
# C2). cancel-payment already reverses its own legs (delink + active mirror) and
# never touches payment_allocation, so releasing a cancelled payment's
# allocation would append a second reversal onto an already-balanced payment and
# turn a correct party ledger red.
_RELEASABLE_PAYMENT_STATUSES = ("submitted",)

# Only a SUBMITTED payment carries a party-level residual to compensate (Wave G
# F2, correction C3). A draft has no party-level ledger row at all and is
# excluded from INV-27's RHS; a cancelled payment nets to exactly zero under the
# reversal-inclusive rule, so it needs no compensation and must not receive one.
_COMPENSABLE_PAYMENT_STATUSES = ("submitted",)


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


def recalc_unallocated(conn, payment_entry_id):
    """Recompute a payment's ``unallocated_amount`` from LIVE detail rows.

    Canonical home of the residual rule (WS2 D3):

        paid_amount = Σ live allocations + Σ deductions + unallocated

    "Live" is the Wave G / M46 half: an allocation released by a document cancel
    carries ``payment_allocation.delinked = 1`` and no longer consumes the
    payment, so the cash returns to the residual. Deductions are NOT reversed by
    a document cancel — a discount/TDS taken at payment time was really taken.

    ``erpclaw-payments._recalc_unallocated`` delegates here so the formula has
    exactly one implementation (the same no-drift rule the rest of this module
    exists for). Runs inside the caller's transaction — does NOT commit.

    Sums in Python Decimal rather than the ``decimal_sum`` aggregate so the lib
    carries no UDF dependency; the row counts here are per-payment and tiny.

    Returns:
        Decimal: the newly written residual, or None if the payment is absent.
    """
    pe_t = Table("payment_entry")
    row = conn.execute(
        Q.from_(pe_t).select(pe_t.paid_amount).where(pe_t.id == P()).get_sql(),
        (payment_entry_id,)).fetchone()
    if row is None:
        return None
    paid = to_decimal(row["paid_amount"])

    alloc_t = Table("payment_allocation")
    q_alloc = (Q.from_(alloc_t).select(alloc_t.allocated_amount)
               .where(alloc_t.payment_entry_id == P())
               .where(alloc_t.delinked == P()))
    allocated = sum(
        (to_decimal(r["allocated_amount"])
         for r in conn.execute(q_alloc.get_sql(), (payment_entry_id, 0))),
        Decimal("0"))

    ded_t = Table("payment_deduction")
    q_ded = (Q.from_(ded_t).select(ded_t.amount)
             .where(ded_t.payment_entry_id == P()))
    deducted = sum(
        (to_decimal(r["amount"])
         for r in conn.execute(q_ded.get_sql(), (payment_entry_id,))),
        Decimal("0"))

    unallocated = round_currency(paid - allocated - deducted)
    conn.execute(
        update_row("payment_entry",
                   data={"unallocated_amount": P(), "updated_at": now()},
                   where={"id": P()}),
        (str(unallocated), payment_entry_id))
    return unallocated


def party_residual_compensation_delta(conn, payment_entry_id):
    """The Fork-A compensation amount still owed for one payment (M38 / W16).

        delta = Σ live payment_allocation.allocated_amount
              + Σ payment_deduction.amount
              − Σ existing compensation rows for this payment

    DETAIL-TABLE DRIVEN, and that is the load-bearing property, not an
    implementation taste. Deriving the amount from
    ``payment_entry.unallocated_amount`` instead would make INV-27's
    LHS ≡ RHS true by construction and blind the invariant to a wrong residual —
    the exact laundering ADR-0032 rejects ("No tautological compensation"). The
    residual column is what INV-27 reads on the OTHER side; the two must be
    computed from independent sources or the check asserts nothing. Negative
    control NC-2 exists to prove it.

    Returns (delta, context) where ``context`` carries the terms so the runtime
    helper, migration 032 and any report can print the arithmetic rather than
    just the answer. Read-only.
    """
    alloc_t = Table("payment_allocation")
    q_alloc = (Q.from_(alloc_t).select(alloc_t.allocated_amount)
               .where(alloc_t.payment_entry_id == P())
               .where(alloc_t.delinked == P()))
    allocated = sum(
        (to_decimal(r["allocated_amount"])
         for r in conn.execute(q_alloc.get_sql(), (payment_entry_id, 0))),
        Decimal("0"))

    ded_t = Table("payment_deduction")
    q_ded = (Q.from_(ded_t).select(ded_t.amount)
             .where(ded_t.payment_entry_id == P()))
    deducted = sum(
        (to_decimal(r["amount"])
         for r in conn.execute(q_ded.get_sql(), (payment_entry_id,))),
        Decimal("0"))

    # Existing compensation is summed REVERSAL-INCLUSIVE, the same reading the
    # invariant's LHS gives payment rows (LIVE_ROW_SQL is a tautology for them;
    # it is spelled out so the two readings are visibly the same one).
    ple_t = Table("payment_ledger_entry")
    q_comp = (Q.from_(ple_t).select(ple_t.amount)
              .where(ple_t.voucher_id == P())
              .where(LiteralValue(COMPENSATION_ROW_SQL))
              .where(LiteralValue(LIVE_ROW_SQL)))
    existing = sum(
        (to_decimal(r["amount"])
         for r in conn.execute(q_comp.get_sql(), (payment_entry_id,))),
        Decimal("0"))

    target = round_currency(allocated + deducted)
    delta = round_currency(target - existing)
    return delta, {"live_allocations": str(round_currency(allocated)),
                   "deductions": str(round_currency(deducted)),
                   "existing_compensation": str(round_currency(existing)),
                   "target": str(target), "delta": str(delta)}


def post_party_residual_compensation(conn, payment_entry_id):
    """Append the party-level residual compensation row for one payment (M38).

    Wave G F2 / ADR-0032 W16 — Fork A, ratified as ruling N1. ``submit_payment``
    writes ONE full-amount party-level row per submit (``against_voucher_*``
    omitted, ``amount = −paid_amount``) AND a per-allocation row per allocation,
    so the same cash is subtracted from the party twice: an invoice of 1,000.00
    paid 300.00 read 400.00 where the truth is 700.00. Fork A corrects the
    LEDGER rather than masking it in each reader, so every future report and
    ad-hoc query is right by default.

    Shape of the appended row (the structural discriminator, SIM correction B5):
    ``voucher_type = 'payment_entry'``, ``voucher_id = <payment>`` AND
    ``against_voucher_type/id`` pointing at that SAME payment. Under
    erpclaw_lib.party_ledger's attribution rule a self-referencing against means
    the row buckets to the payment's own voucher, so it never lands in an
    invoice bucket, and it is identifiable without a new flag column — which is
    what lets migration 032 stay append-only.

    Three guards, each measured into existence by the Wave-G SIM:

    - status (correction C3): fires ONLY for a payment that is
      ``submitted``. Never on a draft — a draft has no party-level row and is
      excluded from the invariant's RHS, so a draft compensation reads RED
      (probe: LHS 1,300 vs RHS 1,000) and ``delete-payment`` would orphan it
      forever.
    - party (correction C9): fires only when the payment carries a party, the
      same guard submit_payment puts on its own party-level row. An
      ``internal_transfer`` is party-less and a NULL-party ledger row would
      break INV-07.
    - delta == 0 (correction C5): writes NOTHING. "Append-only" and "idempotent"
      contradict each other otherwise, and every re-invocation would add a zero
      row. A correctly-compensated payment therefore stays byte-identical across
      re-runs, which is also what makes a mis-healed install self-repair.

    Runs inside the caller's transaction — does NOT commit.

    Returns:
        dict {"payment_entry_id", "written": bool, "reason"|None, "ple_id"|None,
        plus the arithmetic terms from party_residual_compensation_delta}.
    """
    pe_t = Table("payment_entry")
    pe = conn.execute(
        Q.from_(pe_t).select(
            pe_t.id, pe_t.status, pe_t.party_type, pe_t.party_id,
            pe_t.payment_type, pe_t.paid_from_account, pe_t.paid_to_account,
            pe_t.payment_currency, pe_t.posting_date)
        .where(pe_t.id == P()).get_sql(), (payment_entry_id,)).fetchone()
    if pe is None:
        return {"payment_entry_id": payment_entry_id, "written": False,
                "reason": "payment not found"}
    if pe["status"] not in _COMPENSABLE_PAYMENT_STATUSES:
        return {"payment_entry_id": payment_entry_id, "written": False,
                "reason": f"payment is '{pe['status']}' (only 'submitted' "
                          "carries a party-level residual)"}
    if not (pe["party_type"] and pe["party_id"]):
        return {"payment_entry_id": payment_entry_id, "written": False,
                "reason": "payment carries no party (internal transfer)"}

    delta, terms = party_residual_compensation_delta(conn, payment_entry_id)
    out = {"payment_entry_id": payment_entry_id, "written": False,
           "ple_id": None, **terms}
    if delta == Decimal("0"):
        out["reason"] = "delta is 0 — nothing to append"
        return out

    # Same account rule as the party-level row this compensates
    # (erpclaw-payments submit_payment): receivable for 'receive', payable
    # otherwise. Anything else would put the two halves on different accounts.
    account_id = (pe["paid_from_account"] if pe["payment_type"] == "receive"
                  else pe["paid_to_account"])
    amount = str(delta)
    ple_id = str(uuid.uuid4())
    ins_sql, _ = insert_row("payment_ledger_entry", {
        "id": P(), "posting_date": P(), "account_id": P(),
        "party_type": P(), "party_id": P(),
        "voucher_type": P(), "voucher_id": P(),
        "against_voucher_type": P(), "against_voucher_id": P(),
        "amount": P(), "amount_in_account_currency": P(),
        "currency": P(), "remarks": P(),
    })
    conn.execute(ins_sql, (
        ple_id, pe["posting_date"], account_id,
        pe["party_type"], pe["party_id"],
        "payment_entry", payment_entry_id,
        "payment_entry", payment_entry_id,
        amount, amount, pe["payment_currency"],
        "Party-level residual compensation (M38): live allocations "
        f"{terms['live_allocations']} + deductions {terms['deductions']} "
        f"− existing {terms['existing_compensation']}"))
    out["written"] = True
    out["ple_id"] = ple_id
    return out


def _voucher_spellings(voucher_type):
    """The stored spellings that mean this voucher type.

    Rows written since the FINDING-005 write-boundary fix are canonical
    snake_case; older rows can still carry the gateway's label form. Both are
    matched so the release never misses a legacy allocation, and the query stays
    on the (voucher_type, voucher_id) index instead of scanning.
    """
    return sorted({voucher_type, canonical_voucher_type(voucher_type)})


def release_allocations_on_document(conn, voucher_type, voucher_id):
    """Release every live allocation pointing at a document being cancelled.

    Wave G F1 (M46). ``cancel-sales-invoice`` / ``cancel-purchase-invoice`` and
    the two intercompany cancel legs delink the document's OWN payment-ledger
    rows and zero its outstanding, but historically left ``payment_allocation``,
    ``payment_entry.unallocated_amount`` and the per-allocation PLE rows
    untouched — cash stayed "applied" to a document that no longer exists in the
    books, and the payment's residual was understated. This is the mirror of
    reverse_payment_on_document(): there the payment is cancelled and the
    document is restored; here the document is cancelled and the payment is
    restored.

    Per live allocation, in the caller's transaction (this does NOT commit):
      1. mark ``payment_allocation.delinked = 1`` (never DELETE — the row is the
         audit trail; a negative compensating allocation would break every
         reader that assumes a positive allocated_amount),
      2. delink the per-allocation PLE row(s) for that (payment, document) pair
         AND append their reversal mirrors,
      3. recompute the payment's residual from live detail rows,
      4. re-run the party-level residual compensation (Wave G F2 / W16) — the
         released allocation no longer counts, so the delta is negative here.

    Two properties are load-bearing and were both proven red before they were
    specified (planning/simlogs/waveg-plan_SIM_2026-07-31.md findings 1 and 2):

    C1 — the release pair is written with ``delinked = 1`` on BOTH rows. It is
    the only PLE row in the tree written pre-delinked, and that is deliberate:
    a later ``cancel-payment`` selects every ``delinked = 0`` row for the
    payment and mirrors it (erpclaw-payments/db_query.py:1200-1206, :1230-1234),
    so an active release mirror would be reversed a SECOND time and leave the
    party ledger permanently divergent. A closed pair is invisible to that
    generic loop, and it still nets to zero under the reversal-inclusive rule
    payment rows are read with. Do NOT "fix" this to delinked = 0, and do NOT
    teach the cancel loop to skip these rows by remark or shape — a hand-written
    predicate inside a generic loop is the drift this class of bug comes from.

    C2 — allocations whose payment is not 'submitted' are SKIPPED and reported.
    cancel-payment never touches payment_allocation, so a cancelled payment's
    allocation survives with delinked = 0 while its ledger legs are already
    balanced; releasing it would append another reversal onto a correct payment.

    Args:
        conn: open DB connection (caller owns the transaction).
        voucher_type: the document's voucher type as the cancel path knows it
            ('sales_invoice' | 'credit_note' | 'purchase_invoice' |
            'debit_note'). Any type is accepted — allocations against
            non-clearing voucher types are released too, since they consumed
            residual just the same.
        voucher_id: the document id being cancelled.

    Returns:
        dict {"voucher_type", "voucher_id", "released": [...], "skipped": [...]}
        where each ``released`` entry is
        {"payment_entry_id", "allocation_ids", "allocated_amount",
         "ple_rows_released", "unallocated_amount", "residual_compensation"}
        and each ``skipped`` entry is
        {"payment_entry_id", "payment_status", "allocation_ids",
         "allocated_amount"}. Both lists are empty when the document had no live
        allocation, in which case this function writes nothing at all.
    """
    spellings = _voucher_spellings(voucher_type)
    alloc_t = Table("payment_allocation")

    by_payment = {}
    for spelling in spellings:
        q = (Q.from_(alloc_t)
             .select(alloc_t.id, alloc_t.payment_entry_id,
                     alloc_t.allocated_amount)
             .where(alloc_t.voucher_type == P())
             .where(alloc_t.voucher_id == P())
             .where(alloc_t.delinked == P())
             .orderby(alloc_t.created_at).orderby(alloc_t.id))
        for row in conn.execute(q.get_sql(), (spelling, voucher_id, 0)):
            by_payment.setdefault(row["payment_entry_id"], []).append(
                {"id": row["id"],
                 "allocated_amount": to_decimal(row["allocated_amount"])})

    released, skipped = [], []
    if not by_payment:
        return {"voucher_type": voucher_type, "voucher_id": voucher_id,
                "released": released, "skipped": skipped}

    pe_t = Table("payment_entry")
    delink_alloc_sql = update_row("payment_allocation",
                                  data={"delinked": P()}, where={"id": P()})

    for pe_id in sorted(by_payment):
        allocs = by_payment[pe_id]
        total = round_currency(sum((a["allocated_amount"] for a in allocs),
                                   Decimal("0")))
        pe_row = conn.execute(
            Q.from_(pe_t).select(pe_t.status).where(pe_t.id == P()).get_sql(),
            (pe_id,)).fetchone()
        status = pe_row["status"] if pe_row is not None else None
        if status not in _RELEASABLE_PAYMENT_STATUSES:
            # C2: reported, never silently dropped.
            skipped.append({"payment_entry_id": pe_id,
                            "payment_status": status,
                            "allocation_ids": [a["id"] for a in allocs],
                            "allocated_amount": str(total)})
            continue

        for alloc in allocs:
            conn.execute(delink_alloc_sql, (1, alloc["id"]))

        ple_released = _release_allocation_ple(
            conn, pe_id, spellings, voucher_id)
        unallocated = recalc_unallocated(conn, pe_id)
        # Wave G F2: releasing an allocation shrinks Σ live allocations, so the
        # party-level residual compensation must be re-run for this payment —
        # this is one of correction C3's lifecycle sites, and the delta is
        # negative here (it gives back what the earlier compensation added).
        # Guarded on 'submitted' inside the helper too; the C2 skip above means
        # only submitted payments ever reach this line.
        compensation = post_party_residual_compensation(conn, pe_id)
        released.append({"payment_entry_id": pe_id,
                         "allocation_ids": [a["id"] for a in allocs],
                         "allocated_amount": str(total),
                         "ple_rows_released": ple_released,
                         "unallocated_amount": str(unallocated),
                         "residual_compensation": compensation})

    return {"voucher_type": voucher_type, "voucher_id": voucher_id,
            "released": released, "skipped": skipped}


def _release_allocation_ple(conn, payment_entry_id, spellings, voucher_id):
    """Delink a payment's per-allocation PLE rows for one document and mirror
    them, BOTH sides delinked (correction C1 — see the caller's docstring).

    Returns the number of (delink, mirror) pairs written. Rows are paired at the
    (payment, document) level rather than per allocation row because the PLE
    amount carries the allocation PLUS its pro-rata deduction share (the
    "effective" amount at erpclaw-payments/db_query.py:1120-1125), so it cannot
    be re-derived from a single allocation row.
    """
    ple_t = Table("payment_ledger_entry")
    delink_sql = update_row("payment_ledger_entry",
                            data={"delinked": P(), "updated_at": now()},
                            where={"id": P()})
    ins_sql, _ = insert_row("payment_ledger_entry", {
        "id": P(), "posting_date": P(), "account_id": P(),
        "party_type": P(), "party_id": P(),
        "voucher_type": P(), "voucher_id": P(),
        "against_voucher_type": P(), "against_voucher_id": P(),
        "amount": P(), "amount_in_account_currency": P(),
        "currency": P(), "delinked": P(), "remarks": P(),
    })

    pairs = 0
    for spelling in spellings:
        q = (Q.from_(ple_t).select(ple_t.star)
             .where(ple_t.voucher_type == P())
             .where(ple_t.voucher_id == P())
             .where(ple_t.against_voucher_type == P())
             .where(ple_t.against_voucher_id == P())
             .where(ple_t.delinked == P())
             .orderby(ple_t.created_at).orderby(ple_t.id))
        rows = conn.execute(
            q.get_sql(),
            ("payment_entry", payment_entry_id, spelling, voucher_id, 0)
        ).fetchall()
        for row in rows:
            conn.execute(delink_sql, (1, row["id"]))
            reversal = str(round_currency(-to_decimal(row["amount"])))
            conn.execute(ins_sql, (
                str(uuid.uuid4()),
                # The mirror carries the SOURCE row's posting_date so the pair
                # nets to zero inside any as-of-date window an aging report
                # picks, not just at the end of time.
                row["posting_date"], row["account_id"],
                row["party_type"], row["party_id"],
                "payment_entry", payment_entry_id,
                row["against_voucher_type"], row["against_voucher_id"],
                reversal, reversal, row["currency"], 1,
                f"Release: allocation voided by cancel of "
                f"{row['against_voucher_type']} {row['against_voucher_id']}"))
            pairs += 1
    return pairs
