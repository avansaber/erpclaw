"""Migration 031: payment_allocation.delinked + release back-heal (Wave G F1 / M46).

Two halves, in this order:

  1. ADD COLUMN `payment_allocation.delinked` (idempotent existence probe, both
     dialects). Same vocabulary payment_ledger_entry.delinked already uses.
  2. BACK-HEAL every install that already ran the defect: for each LIVE
     allocation pointing at a CANCELLED document whose payment is still
     'submitted', delink the allocation, write the per-allocation PLE release
     pair with `delinked = 1` on BOTH rows, and recompute the payment's
     `unallocated_amount` from live detail rows.

Why both sides of the release pair are pre-delinked (correction C1): a later
`cancel-payment` mirrors every `delinked = 0` row of the payment
(erpclaw-payments/db_query.py:1200-1206, :1230-1234). An ACTIVE release mirror
would be reversed a second time and leave that party permanently divergent. A
closed pair is invisible to that generic loop and still nets to zero under the
reversal-inclusive rule payment rows are read with.

Why a non-'submitted' payment is skipped and REPORTED (correction C2):
`cancel-payment` already reverses its own legs and never touches
payment_allocation, so a cancelled payment's allocation survives with
`delinked = 0` while its ledger is already balanced. Releasing it would append
a second reversal and turn a correct party red.

This is the migration-side transcription of
`erpclaw_lib.payment_clearing.release_allocations_on_document` (runtime side).
The two must stay in step; each file names the other. One deliberate difference:
the runtime helper also re-runs Wave G F2's party-residual compensation, which is
a NO-OP BY CONSTRUCTION here — no compensating row can exist before migration
032 runs, and 031 must run first (032's precondition reads LIVE allocations, and
031 is what makes an allocation against a cancelled document non-live).

Money is Decimal over TEXT throughout; no float anywhere. `--report-only`
enumerates exactly what the real run would change and leaves the DB
byte-identical. Idempotent: after a real run there is no live allocation against
a cancelled document left to heal, so a re-run reports and writes nothing.

AUDIT TRAIL (M102). Every payment this run heals gets ONE `audit_log` row naming
the allocations it delinked and the residual before and after — the same grain
`submit-payment` and `cancel-payment` audit at, which is the document rather than
each ledger leg. Written on the SAME cursor inside the SAME transaction as the
heal, so a rolled-back report mode and a crashed run both leave no trail, and a
committed heal always has one. Read it back with

    get-audit-log --audit-action "migration:031_allocation_delink_and_release"

Without it the release is invisible after the terminal output is gone: the
allocation ids this run voided are the one fact a reversal needs and the one
fact nothing else records. Convention + gate:
planning/simlogs/m102_SIM_2026-08-12.md.
"""
import argparse
import importlib.util
import os
import sqlite3
import sys
import uuid
from decimal import Decimal, ROUND_HALF_UP

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib.audit import migration_action, migration_audit_statement  # noqa: E402

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

# M102: derived from the filename, never typed, so the trail's action string
# cannot drift from the stem migration_runner ledgers this file under.
MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]

# Rewrites payment_allocation.delinked, payment_ledger_entry.delinked and
# payment_entry.unallocated_amount on rows selected from the install's own data,
# and appends release mirrors computed from them (M102 §3).
MIGRATION_DATA_CLASS = "rows"

_COLDEF = "INTEGER NOT NULL DEFAULT 0 CHECK(delinked IN (0,1))"
_CENT = Decimal("0.01")

# Documents whose cancel voids an allocation. Both the base type and the return
# type are covered: an allocation row stores whichever voucher_type the payment
# named. One fully-static SELECT per document table — the table name is part of
# the statement, never interpolated, so nothing but bound parameters ever
# reaches the database.
_DOC_VOUCHER_TYPES = {
    "sales_invoice": ("sales_invoice", "credit_note"),
    "purchase_invoice": ("purchase_invoice", "debit_note"),
}

_SCAN_SALES_HEAD = """
SELECT pa.id, pa.payment_entry_id, pa.voucher_type, pa.voucher_id,
       pa.allocated_amount, pe.status
  FROM payment_allocation pa
  JOIN sales_invoice d ON d.id = pa.voucher_id
  LEFT JOIN payment_entry pe ON pe.id = pa.payment_entry_id
 WHERE pa.voucher_type IN (?, ?)
   AND d.status = 'cancelled'
"""

_SCAN_PURCHASE_HEAD = """
SELECT pa.id, pa.payment_entry_id, pa.voucher_type, pa.voucher_id,
       pa.allocated_amount, pe.status
  FROM payment_allocation pa
  JOIN purchase_invoice d ON d.id = pa.voucher_id
  LEFT JOIN payment_entry pe ON pe.id = pa.payment_entry_id
 WHERE pa.voucher_type IN (?, ?)
   AND d.status = 'cancelled'
"""

# The liveness clause is split out for ONE reason: on a pre-031 database the
# column does not exist yet, and every allocation there is live by definition.
# Report mode on such a DB drops the clause so it can still enumerate exactly
# the rows the real run will heal — otherwise "--report-only first", the one
# workflow the plan mandates, could never list anything on the installs that
# actually need the heal. Static fragments, concatenated; nothing is formatted
# into SQL.
_LIVE_ONLY = "   AND pa.delinked = 0\n"
_SCAN_TAIL = " ORDER BY pa.payment_entry_id, pa.created_at, pa.id\n"

_SCANS = (
    ("sales_invoice", _SCAN_SALES_HEAD + _LIVE_ONLY + _SCAN_TAIL,
     _SCAN_SALES_HEAD + _SCAN_TAIL),
    ("purchase_invoice", _SCAN_PURCHASE_HEAD + _LIVE_ONLY + _SCAN_TAIL,
     _SCAN_PURCHASE_HEAD + _SCAN_TAIL),
)

_SELECT_ALLOC_PLE = """
SELECT id, posting_date, account_id, party_type, party_id,
       against_voucher_type, against_voucher_id, amount, currency
  FROM payment_ledger_entry
 WHERE voucher_type = ? AND voucher_id = ?
   AND against_voucher_type = ? AND against_voucher_id = ?
   AND delinked = 0
 ORDER BY created_at, id
"""

_DELINK_PLE = "UPDATE payment_ledger_entry SET delinked = 1 WHERE id = ?"

_INSERT_PLE = """
INSERT INTO payment_ledger_entry
    (id, posting_date, account_id, party_type, party_id,
     voucher_type, voucher_id, against_voucher_type, against_voucher_id,
     amount, amount_in_account_currency, currency, delinked, remarks)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
"""

_DELINK_ALLOC = "UPDATE payment_allocation SET delinked = 1 WHERE id = ?"
_SELECT_PAID = ("SELECT paid_amount, unallocated_amount FROM payment_entry "
                "WHERE id = ?")
_SELECT_LIVE_ALLOC = ("SELECT allocated_amount FROM payment_allocation "
                      "WHERE payment_entry_id = ? AND delinked = 0")
_SELECT_DEDUCTIONS = ("SELECT amount FROM payment_deduction "
                      "WHERE payment_entry_id = ?")
_UPDATE_RESIDUAL = "UPDATE payment_entry SET unallocated_amount = ? WHERE id = ?"


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _dec(value):
    return Decimal(str(value)) if value not in (None, "") else Decimal("0")


def _money(value):
    return _dec(value).quantize(_CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Back-heal (dialect-neutral: takes a DB-API cursor + that dialect's paramstyle)
# ---------------------------------------------------------------------------

def _bind(sql, ph):
    """Bind the active paramstyle into a static SQL template.

    Every statement in this file is a fixed string written with SQLite's '?'
    placeholder; the ONLY substitution that ever happens is '?' -> '%s' for
    psycopg2. No value, table name or column name is ever formatted into SQL —
    one auditable seam instead of a dozen f-strings.
    """
    return sql if ph == "?" else sql.replace("?", ph)


def _scan(cur, ph, has_column=True):
    """Return (heal, skips) for every live allocation against a cancelled doc.

    heal:  {payment_entry_id: [ {allocation row}, ... ]} — payment is submitted
    skips: [ {reason, payment_entry_id, payment_status, allocation_id, ...} ]
    Read-only: this is exactly what --report-only prints and what the real run
    then applies, so the two can never describe different work.

    ``has_column=False`` is the pre-031 report case: no delinked column yet, so
    every allocation is live and the filter is dropped rather than crashing.
    """
    heal, skips = {}, []
    for doc_table, live_sql, precolumn_sql in _SCANS:
        scan_sql = live_sql if has_column else precolumn_sql
        cur.execute(_bind(scan_sql, ph), _DOC_VOUCHER_TYPES[doc_table])
        for row in cur.fetchall():
            (alloc_id, pe_id, voucher_type, voucher_id, amount,
             pay_status) = row
            entry = {"allocation_id": alloc_id, "payment_entry_id": pe_id,
                     "voucher_type": voucher_type, "voucher_id": voucher_id,
                     "allocated_amount": str(_money(amount)),
                     "payment_status": pay_status}
            if pay_status != "submitted":
                entry["reason"] = ("payment is "
                                   f"'{pay_status}' (only 'submitted' is released)")
                skips.append(entry)
                continue
            heal.setdefault(pe_id, []).append(entry)
    return heal, skips


def _release_ple(cur, ph, pe_id, voucher_type, voucher_id):
    """Delink the (payment, document) per-allocation PLE rows and mirror them,
    both sides delinked. Returns the number of pairs written."""
    cur.execute(_bind(_SELECT_ALLOC_PLE, ph),
                ("payment_entry", pe_id, voucher_type, voucher_id))
    rows = cur.fetchall()
    pairs = 0
    for (ple_id, posting_date, account_id, party_type, party_id,
         avt, avid, amount, currency) in rows:
        cur.execute(_bind(_DELINK_PLE, ph), (ple_id,))
        reversal = str(_money(-_dec(amount)))
        cur.execute(_bind(_INSERT_PLE, ph), (
            str(uuid.uuid4()), posting_date, account_id, party_type, party_id,
            "payment_entry", pe_id, avt, avid, reversal, reversal, currency,
            f"Release: allocation voided by cancel of {avt} {avid}"))
        pairs += 1
    return pairs


def _recalc_unallocated(cur, ph, pe_id):
    """paid_amount − Σ LIVE allocations − Σ deductions, written back as TEXT.

    Returns ``(previous, recomputed)``. The previous value is read in the same
    statement that reads ``paid_amount`` — one row, not a second query — because
    the audit trail (M102) has to say what the residual WAS, and reading it after
    the write would report the new value as the old one.
    """
    cur.execute(_bind(_SELECT_PAID, ph), (pe_id,))
    row = cur.fetchone()
    if row is None:
        return None, None
    paid = _dec(row[0])
    previous = row[1]
    cur.execute(_bind(_SELECT_LIVE_ALLOC, ph), (pe_id,))
    allocated = sum((_dec(r[0]) for r in cur.fetchall()), Decimal("0"))
    cur.execute(_bind(_SELECT_DEDUCTIONS, ph), (pe_id,))
    deducted = sum((_dec(r[0]) for r in cur.fetchall()), Decimal("0"))
    unallocated = _money(paid - allocated - deducted)
    cur.execute(_bind(_UPDATE_RESIDUAL, ph), (str(unallocated), pe_id))
    return previous, unallocated


def _back_heal(cur, ph, report_only, has_column=True):
    """Apply (or, in report mode, only enumerate) the release back-heal."""
    heal, skips = _scan(cur, ph, has_column=has_column)
    healed = []
    for pe_id in sorted(heal):
        allocs = heal[pe_id]
        total = _money(sum((_dec(a["allocated_amount"]) for a in allocs),
                           Decimal("0")))
        if report_only:
            healed.append({"payment_entry_id": pe_id,
                           "allocations": [a["allocation_id"] for a in allocs],
                           "allocated_amount": str(total)})
            continue
        pairs = 0
        for alloc in allocs:
            cur.execute(_bind(_DELINK_ALLOC, ph), (alloc["allocation_id"],))
            pairs += _release_ple(cur, ph, pe_id, alloc["voucher_type"],
                                  alloc["voucher_id"])
        previous, unallocated = _recalc_unallocated(cur, ph, pe_id)
        # M102 — same cursor, same transaction as every write above. One row per
        # healed PAYMENT, not per delinked allocation: that is the grain
        # submit-payment and cancel-payment already audit at, and the allocation
        # ids a reversal needs travel in the values.
        sql, params = migration_audit_statement(
            MIGRATION_ID, "payment_entry", pe_id,
            old_values={"unallocated_amount": previous,
                        "live_allocations": [a["allocation_id"] for a in allocs]},
            new_values={"unallocated_amount": str(unallocated),
                        "delinked_allocations": [a["allocation_id"] for a in allocs],
                        "ple_release_pairs": pairs},
            description="migration %s released %d allocation(s) totalling %s "
                        "against cancelled document(s) and recomputed the "
                        "residual %s -> %s"
                        % (MIGRATION_ID, len(allocs), total, previous,
                           unallocated))
        cur.execute(_bind(sql, ph), params)
        healed.append({"payment_entry_id": pe_id,
                       "allocations": [a["allocation_id"] for a in allocs],
                       "allocated_amount": str(total),
                       "ple_pairs": pairs,
                       "unallocated_amount": str(unallocated)})
    return healed, skips


def _print_summary(healed, skips, report_only):
    verb = "would release" if report_only else "released"
    print(f"  payment_allocation back-heal: {verb} {len(healed)} payment(s) "
          f"with allocations against cancelled documents.")
    for h in healed:
        detail = ""
        if not report_only:
            detail = (f", {h['ple_pairs']} PLE release pair(s), "
                      f"unallocated -> {h['unallocated_amount']}")
        # Allocation ids are listed, not just counted: the report is the
        # operator's review artifact before the real run, and a count cannot be
        # checked against the ledger.
        print(f"    payment {h['payment_entry_id']}: "
              f"{len(h['allocations'])} allocation(s) "
              f"[{', '.join(h['allocations'])}], "
              f"{h['allocated_amount']}{detail}")
    # Skips are REPORTED, never silent (correction C2).
    print(f"  skipped: {len(skips)} allocation(s) whose payment is not submitted.")
    for s in skips:
        print(f"    allocation {s['allocation_id']} (payment "
              f"{s['payment_entry_id']}): {s['reason']}")
    if healed and not report_only:
        print(f"  audit trail: {len(healed)} audit_log row(s), committed with the "
              f"heal. Read them back with:  get-audit-log --audit-action "
              f'"{migration_action(MIGRATION_ID)}"')
    elif healed:
        print(f"  report-only: no audit_log row is written — a trail for a change "
              f"that did not happen would be the lie M102 exists to prevent. The "
              f"real run writes {len(healed)}.")


# ---------------------------------------------------------------------------
# Dialect entry points
# ---------------------------------------------------------------------------

def _run_sqlite(path, report_only=False):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")

    if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='payment_allocation'").fetchone():
        print("  payment_allocation: table absent. Nothing to migrate.")
        conn.close()
        return

    has_column = _sqlite_has_column(conn, "payment_allocation", "delinked")
    if report_only:
        print("  payment_allocation.delinked: "
              + ("already present." if has_column
                 else "would be added (ALTER TABLE ... ADD COLUMN)."))
    elif has_column:
        print("  payment_allocation.delinked: already present.")
    else:
        conn.execute(
            f"ALTER TABLE payment_allocation ADD COLUMN delinked {_COLDEF}")
        conn.commit()
        has_column = True
        print("  payment_allocation.delinked: added.")

    cur = conn.cursor()
    healed, skips = _back_heal(cur, "?", report_only, has_column=has_column)
    if report_only:
        conn.rollback()
    else:
        conn.commit()
    _print_summary(healed, skips, report_only)
    conn.close()


def _run_postgres(url, report_only=False):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            # Report mode never runs DDL — not even a rolled-back ALTER (it
            # would still take the table lock). Probe the catalog instead.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
                ("payment_allocation", "delinked"))
            has_column = cur.fetchone() is not None
            if report_only:
                print("  Postgres: payment_allocation.delinked "
                      + ("already present."
                         if has_column else "would be added."))
            else:
                cur.execute(
                    "ALTER TABLE payment_allocation ADD COLUMN IF NOT EXISTS "
                    f"delinked {_COLDEF}")
                conn.commit()
                has_column = True
                print("  Postgres: payment_allocation.delinked ensured.")

        with conn.cursor() as cur:
            healed, skips = _back_heal(cur, "%s", report_only,
                                       has_column=has_column)
        if report_only:
            conn.rollback()
        else:
            conn.commit()
        _print_summary(healed, skips, report_only)
    finally:
        conn.close()


def run_migration(db_path=None, report_only=False):
    if _get_dialect() == "postgresql":
        url = os.environ.get("ERPCLAW_DB_URL") or db_path
        if not url:
            print("Postgres dialect set but no connection URL (ERPCLAW_DB_URL). Nothing to migrate.")
            return
        _run_postgres(url, report_only=report_only)
        return
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(path):
        print(f"Database not found at {path}. Nothing to migrate.")
        return
    _run_sqlite(path, report_only=report_only)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 031: payment_allocation.delinked + release back-heal")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="Enumerate what the real run would change; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("Migration 031 "
          + ("report complete (no writes)." if args.report_only else "complete."))
