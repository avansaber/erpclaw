"""Canonical party-level payment-ledger reading (ADR-0032 Decision 2).

ONE home for the two rules every consumer that aggregates
``payment_ledger_entry`` (PLE) must use — liveness and attribution. The
precedent is ``erpclaw_lib/gl_invariants.py``: a cross-skill home for a shared
predicate, so no module hand-copies it.

Consumers (all import from here; a fourth guarded copy is described below):
  - erpclaw-reports ``_aging_report``  (ar-aging / ap-aging, both queries)
  - erpclaw-payments ``get_outstanding``
  - erpclaw_lib.payment_clearing's party-residual compensation (discriminator)
  - testing/invariant_engine.py INV-27 — a VERBATIM COPY of the block below,
    because the engine is deliberately stdlib-only and must not import from
    ~/.openclaw/erpclaw/lib. An L0 drift-guard test
    (testing/unit/L0/test_party_ledger_predicate_sync.py) asserts byte-equality
    of the two copies AND that the readers above genuinely import this module
    instead of carrying an inline predicate. Dropping that guard invalidates the
    single-source argument this module exists for (ADR-0032 Consequences).

Why two rules and not one flat filter — the mistake this module removes:

  A flat ``delinked = 0`` party sum goes RED after every ``cancel-payment``.
  The shipped cancel helpers are ASYMMETRIC: an invoice cancel delinks its own
  rows IN PLACE and writes no reversal (erpclaw-selling ``cancel_sales_invoice``,
  erpclaw-buying ``cancel_purchase_invoice``), while a payment cancel delinks AND
  writes an ACTIVE mirror (erpclaw-payments ``cancel_payment``). Netting the
  payment pair is the only reading that returns the right answer, and dropping
  the delinked document row is the only reading that returns the right answer on
  the other side. Measured, not reasoned: INV-25's docstring
  (testing/invariant_engine.py), planning/simlogs/wavef-s14-inv25_SIM_2026-07-25.md
  item 4, ADR-0030 (INV-24), ADR-0031:35, ADR-0032 Decision 2.

Everything here is a pure predicate — no writes, no transaction, no commit. The
SQL fragments are *expressions*, never whole statements, so they compose into a
PyPika query (``LiteralValue``) or a hand-written stdlib ``sqlite3`` statement
without carrying a paramstyle. They use double-quoted identifiers and
single-quoted literals: valid on SQLite and PostgreSQL alike.
"""
from erpclaw_lib.vendor.pypika.terms import LiteralValue

# ── BEGIN CANONICAL PARTY-LEDGER PREDICATE ───────────────────────────────────
# VERBATIM COPY LIVES IN testing/invariant_engine.py — the L0 drift guard
# (testing/unit/L0/test_party_ledger_predicate_sync.py) compares the two blocks
# byte for byte. Edit BOTH or the gate goes red; never edit only one.
#
# LIVENESS (ADR-0032 Decision 2). A row counts toward a party's net when:
#   - it is a payment row (voucher_type = 'payment_entry'): counted
#     REVERSAL-INCLUSIVE, with NO delinked filter, because cancel-payment
#     delinks a row AND inserts its active mirror — only the pair nets right; or
#   - it is a document row (any other voucher_type) that is still live
#     (delinked = 0), because invoice cancel delinks in place and writes no
#     reversal, so a delinked document row must drop out.
LIVE_ROW_SQL = """("voucher_type" = 'payment_entry' OR "delinked" = 0)"""

# ATTRIBUTION (ADR-0032 Decision 2, correction C6). A row's bucket is its
# against-voucher when it is a payment row AND the against-voucher is PRESENT
# and is not the row's own voucher; otherwise the row's own voucher.
#
# The word PRESENT is load-bearing and was measured: submit_payment's
# party-level row omits against_voucher_* entirely, and NULL "is not the payment
# itself", so a rule without the presence test buckets every such row to a
# phantom (None, None) voucher — party totals stay right while the per-voucher
# output of ar-aging and get-outstanding goes wrong.
#
# The self-reference test is what keeps the F2 residual-compensation row
# (voucher = against = the payment) in the payment's own bucket, and it is also
# what makes a document's own row always count to itself — a credit note's row
# stays on the credit note, which is INV-25's stated design.
_ATTRIBUTED_TO_AGAINST_SQL = (
    """"voucher_type" = 'payment_entry'"""
    """ AND "against_voucher_type" IS NOT NULL"""
    """ AND "against_voucher_id" IS NOT NULL"""
    """ AND NOT ("against_voucher_type" = "voucher_type\""""
    """ AND "against_voucher_id" = "voucher_id")"""
)
BUCKET_VOUCHER_TYPE_SQL = (
    f"""CASE WHEN {_ATTRIBUTED_TO_AGAINST_SQL}"""
    """ THEN "against_voucher_type" ELSE "voucher_type" END"""
)
BUCKET_VOUCHER_ID_SQL = (
    f"""CASE WHEN {_ATTRIBUTED_TO_AGAINST_SQL}"""
    """ THEN "against_voucher_id" ELSE "voucher_id" END"""
)

# The structural discriminator of a party-residual compensation row (Fork A,
# ADR-0032 W16): it is written under the payment's own voucher AND points its
# against-voucher at that same payment. The invoice writers already use a
# self/original against-reference, so this is an existing idiom rather than a new
# convention — and it is why the migration-032 heal can be append-only: a
# compensation row is identifiable without a flag column.
#
# USE IT POSITIVELY. `WHERE ... AND COMPENSATION_ROW_SQL` is correct: a row whose
# against_voucher_* is NULL yields NULL, which behaves as false, so the
# party-level row is not mistaken for a compensation. Negating it (`AND NOT
# (...)`) is NOT the complement — NULL stays NULL and drops the party-level row
# from the result set instead of keeping it. Wrap the columns in COALESCE if you
# ever need the complement.
COMPENSATION_ROW_SQL = (
    """("voucher_type" = 'payment_entry'"""
    """ AND "against_voucher_type" = 'payment_entry'"""
    """ AND "against_voucher_id" = "voucher_id")"""
)


def is_live_row(voucher_type, delinked):
    """Python form of LIVE_ROW_SQL, for consumers that iterate rows."""
    return voucher_type == "payment_entry" or int(delinked or 0) == 0


def bucket_of(voucher_type, voucher_id, against_voucher_type,
              against_voucher_id):
    """Python form of the attribution rule. Returns (voucher_type, voucher_id).

    Never returns (None, None): a row always falls back to its own voucher,
    both of whose columns are NOT NULL.
    """
    if (voucher_type == "payment_entry"
            and against_voucher_type is not None
            and against_voucher_id is not None
            and not (against_voucher_type == voucher_type
                     and against_voucher_id == voucher_id)):
        return (against_voucher_type, against_voucher_id)
    return (voucher_type, voucher_id)
# ── END CANONICAL PARTY-LEDGER PREDICATE ─────────────────────────────────────


# ── PyPika-composable forms (runtime consumers only; not part of the block) ──
# The invariant engine builds stdlib sqlite3 statements around the raw fragments
# above, so these wrappers deliberately live OUTSIDE the guarded block: the
# engine must never need PyPika to carry the predicate.

def live_rows_criterion():
    """The liveness rule as a PyPika criterion: ``.where(live_rows_criterion())``."""
    return LiteralValue(LIVE_ROW_SQL)


def bucket_voucher_type_term():
    """The attributed voucher_type as a PyPika selectable term."""
    return LiteralValue(BUCKET_VOUCHER_TYPE_SQL)


def bucket_voucher_id_term():
    """The attributed voucher_id as a PyPika selectable term."""
    return LiteralValue(BUCKET_VOUCHER_ID_SQL)
