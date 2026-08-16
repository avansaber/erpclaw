"""Migration 032: heal the party-level PLE double count (Wave G F2 / M38).

APPEND-ONLY. This migration never rewrites an existing ``amount``; it appends a
compensating payment_ledger_entry row per submitted payment that needs one. That
property is load-bearing, not stylistic: the historical party-level rows keep
their shape and their audit trail, which is what lets the runtime writer stay
unchanged (ADR-0032 W5/W7) and what makes the heal reviewable.

The defect it heals: ``submit-payment`` writes ONE full-amount party-level row
per submit AND a per-allocation row per allocation, so the same cash is
subtracted from the party twice. A customer with a 1,000.00 invoice who paid
300.00 read 400.00 where the truth is 700.00.

RUNS AFTER 031, and the order is a dependency that was demonstrated rather than
argued. 032's precondition reads LIVE allocations, and 031 is what makes an
allocation against a cancelled document non-live. Running 032 first mis-heals
precisely the installs carrying the F1 defect: the precondition passes on a
defect install (0 == 300 − 300 − 0 while the allocation is still live) and a
compensation is appended for an allocation that 031 then delinks. Filenames sort,
so migration_runner applies 031 first; this file additionally refuses to run at
all if 031's column is absent rather than guessing.

DELTA ARITHMETIC, not "payments lacking a compensation" (correction C5). The
target set is EVERY submitted payment, and the amount written is

    delta = Σ live payment_allocation.allocated_amount
          + Σ payment_deduction.amount
          − Σ existing compensation rows for this payment

with **delta == 0 ⇒ NOTHING IS WRITTEN**. Two consequences, both required:
a correctly-healed install stays byte-identical across re-runs (true idempotency,
not an accumulation of zero rows), and a mis-healed install SELF-REPAIRS on the
next run — under the discarded "lacking a row" reading the payment no longer
"lacks" one, so the wrong figure would have been permanent.

NEVER DERIVED FROM unallocated_amount. The amount comes from the DETAIL tables.
Deriving it from the residual column would make INV-27's LHS ≡ RHS true by
construction and blind the invariant to a wrong residual — the exact laundering
ADR-0032 rejects. The residual column appears here only as a PRECONDITION.

REFUSES TO HEAL WHAT IT CANNOT PROVE. Per payment, the stored residual must
already agree with the detail:

    unallocated_amount == paid_amount − Σ live allocations − Σ deductions

The deduction term is not optional (sprint-2 correction B4): without it every
deduction-carrying payment fails the check and is skipped, and the always-on
invariant then reds on live data. A payment that fails the precondition is
SKIPPED AND REPORTED — never touched, never laundered into agreement.

Party-less payments (``internal_transfer``) are skipped: they have no
party-level row to compensate, and payment_ledger_entry.party_type/party_id are
NOT NULL (correction C9).

This is the migration-side transcription of
``erpclaw_lib.payment_clearing.post_party_residual_compensation`` (runtime side),
whose row shape and discriminator come from ``erpclaw_lib.party_ledger``. The two
must stay in step; each file names the other.

Money is Decimal over TEXT throughout; no float anywhere. ``--report-only``
enumerates exactly what the real run would change and leaves the DB
byte-identical.

AUDIT TRAIL (M102). Every payment this run compensates gets ONE ``audit_log`` row
naming the amount appended and the three terms it came from, at the same grain
``submit-payment`` audits at (the payment, not each ledger leg). Written on the
SAME cursor inside the SAME transaction as ``_apply``, so report mode and a
crashed run leave no trail and a committed heal always has one. Because the delta
arithmetic writes nothing when delta is zero, a re-run on a healed install
appends no PLE row and no audit row: the trail is idempotent for exactly the
reason the heal is. Read it back with

    get-audit-log --audit-action "migration:032_heal_party_level_ple"

Convention + gate: planning/simlogs/m102_SIM_2026-08-12.md.
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

# Appends payment_ledger_entry rows whose amount is computed from this install's
# own allocations and deductions (M102 §3).
MIGRATION_DATA_CLASS = "rows"

_CENT = Decimal("0.01")

_SELECT_PAYMENTS = """
SELECT id, party_type, party_id, payment_type, paid_from_account,
       paid_to_account, payment_currency, posting_date, paid_amount,
       unallocated_amount
  FROM payment_entry
 WHERE status = 'submitted'
 ORDER BY created_at, id
"""

_SELECT_LIVE_ALLOC = ("SELECT allocated_amount FROM payment_allocation "
                      "WHERE payment_entry_id = ? AND delinked = 0")
_SELECT_DEDUCTIONS = ("SELECT amount FROM payment_deduction "
                      "WHERE payment_entry_id = ?")

# The compensation discriminator: the row sits under the payment's own voucher
# AND points its against-voucher at that same payment (erpclaw_lib.party_ledger
# COMPENSATION_ROW_SQL). No delinked filter — payment rows are read
# reversal-inclusive, the same reading the invariant's LHS gives them.
_SELECT_EXISTING_COMP = """
SELECT amount FROM payment_ledger_entry
 WHERE voucher_type = 'payment_entry' AND voucher_id = ?
   AND against_voucher_type = 'payment_entry' AND against_voucher_id = ?
"""

_INSERT_COMP = """
INSERT INTO payment_ledger_entry
    (id, posting_date, account_id, party_type, party_id,
     voucher_type, voucher_id, against_voucher_type, against_voucher_id,
     amount, amount_in_account_currency, currency, delinked, remarks)
VALUES (?, ?, ?, ?, ?, 'payment_entry', ?, 'payment_entry', ?, ?, ?, ?, 0, ?)
"""


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _dec(value):
    return Decimal(str(value)) if value not in (None, "") else Decimal("0")


def _money(value):
    return _dec(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def _bind(sql, ph):
    """Bind the active paramstyle into a static SQL template.

    Every statement in this file is a fixed string written with SQLite's '?'
    placeholder; the ONLY substitution that ever happens is '?' -> '%s' for
    psycopg2. No value, table name or column name is ever formatted into SQL.
    """
    return sql if ph == "?" else sql.replace("?", ph)


def _sum(cur, ph, sql, params):
    cur.execute(_bind(sql, ph), params)
    return sum((_dec(r[0]) for r in cur.fetchall()), Decimal("0"))


def _plan(cur, ph):
    """Compute, read-only, exactly what the real run would append.

    Returns (writes, skips). ``--report-only`` prints this and stops; the real
    run prints the same thing and applies it, so the two can never describe
    different work.
    """
    cur.execute(_bind(_SELECT_PAYMENTS, ph), ())
    payments = cur.fetchall()

    writes, skips = [], []
    for (pe_id, party_type, party_id, payment_type, paid_from, paid_to,
         currency, posting_date, paid_amount, unallocated) in payments:
        if not (party_type and party_id):
            skips.append({"payment_entry_id": pe_id,
                          "reason": "payment carries no party (internal transfer)"})
            continue

        allocated = _sum(cur, ph, _SELECT_LIVE_ALLOC, (pe_id,))
        deducted = _sum(cur, ph, _SELECT_DEDUCTIONS, (pe_id,))

        # Deduction-aware precondition (B4). Refuse to heal what we cannot
        # prove: if the stored residual already disagrees with the detail rows,
        # this payment has a different problem and appending a compensation
        # would launder it into a wrong-but-consistent number.
        expected = _money(_dec(paid_amount) - allocated - deducted)
        actual = _money(unallocated)
        if expected != actual:
            skips.append({
                "payment_entry_id": pe_id,
                "reason": (f"residual {actual} disagrees with paid "
                           f"{_money(paid_amount)} − live allocations "
                           f"{_money(allocated)} − deductions "
                           f"{_money(deducted)} = {expected}")})
            continue

        existing = _sum(cur, ph, _SELECT_EXISTING_COMP, (pe_id, pe_id))
        delta = _money(allocated + deducted - existing)
        if delta == Decimal("0"):
            continue  # C5: delta 0 writes nothing at all

        writes.append({
            "payment_entry_id": pe_id, "party_type": party_type,
            "party_id": party_id, "posting_date": posting_date,
            "currency": currency,
            "account_id": paid_from if payment_type == "receive" else paid_to,
            "live_allocations": str(_money(allocated)),
            "deductions": str(_money(deducted)),
            "existing_compensation": str(_money(existing)),
            "amount": str(delta)})
    return writes, skips


def _apply(cur, ph, writes):
    for w in writes:
        ple_id = str(uuid.uuid4())
        cur.execute(_bind(_INSERT_COMP, ph), (
            ple_id, w["posting_date"], w["account_id"],
            w["party_type"], w["party_id"], w["payment_entry_id"],
            w["payment_entry_id"], w["amount"], w["amount"], w["currency"],
            "Party-level residual compensation (M38, migration 032): live "
            f"allocations {w['live_allocations']} + deductions "
            f"{w['deductions']} − existing {w['existing_compensation']}"))
        # M102 — same cursor, same transaction as the INSERT above. The values
        # are the ones that drove the write, so the trail and the row cannot
        # disagree, and the appended PLE id is named so a reversal has its target.
        sql, params = migration_audit_statement(
            MIGRATION_ID, "payment_entry", w["payment_entry_id"],
            new_values={"payment_ledger_entry_id": ple_id,
                        "amount": w["amount"],
                        "live_allocations": w["live_allocations"],
                        "deductions": w["deductions"],
                        "existing_compensation": w["existing_compensation"]},
            description="migration %s appended a party-level compensation of %s "
                        "(allocations %s + deductions %s − existing %s) as "
                        "payment_ledger_entry %s"
                        % (MIGRATION_ID, w["amount"], w["live_allocations"],
                           w["deductions"], w["existing_compensation"], ple_id))
        cur.execute(_bind(sql, ph), params)


def _print_summary(writes, skips, report_only):
    verb = "would append" if report_only else "appended"
    print(f"  party-level PLE heal: {verb} {len(writes)} compensation row(s).")
    for w in writes:
        # Amounts and terms are printed, not just counted: the report is the
        # operator's review artifact before the real run, and a count cannot be
        # checked against the ledger.
        print(f"    payment {w['payment_entry_id']}: {w['amount']} "
              f"(allocations {w['live_allocations']} + deductions "
              f"{w['deductions']} − existing {w['existing_compensation']})")
    # Skips are REPORTED, never silent.
    print(f"  skipped: {len(skips)} payment(s) this migration refuses to heal.")
    for s in skips:
        print(f"    payment {s['payment_entry_id']}: {s['reason']}")
    if writes and not report_only:
        print(f"  audit trail: {len(writes)} audit_log row(s), committed with the "
              f"heal. Read them back with:  get-audit-log --audit-action "
              f'"{migration_action(MIGRATION_ID)}"')
    elif writes:
        print(f"  report-only: no audit_log row is written — a trail for a change "
              f"that did not happen would be the lie M102 exists to prevent. The "
              f"real run writes {len(writes)}.")


def _has_delinked_column_sqlite(conn):
    return any(r[1] == "delinked"
               for r in conn.execute("PRAGMA table_info(payment_allocation)"))


_ORDER_ERROR = (
    "  payment_allocation.delinked is absent — migration 031 has not run.\n"
    "  032 reads LIVE allocations and 031 is what makes an allocation against a\n"
    "  cancelled document non-live; running 032 first MIS-HEALS exactly the\n"
    "  installs that carry the F1 defect. Nothing written. Run 031 first.")


def _run_sqlite(path, report_only=False):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")

    for table in ("payment_entry", "payment_allocation", "payment_ledger_entry"):
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone():
            print(f"  {table}: table absent. Nothing to migrate.")
            conn.close()
            return

    if not _has_delinked_column_sqlite(conn):
        print(_ORDER_ERROR)
        conn.close()
        return

    cur = conn.cursor()
    writes, skips = _plan(cur, "?")
    if report_only:
        conn.rollback()
    else:
        _apply(cur, "?", writes)
        conn.commit()
    _print_summary(writes, skips, report_only)
    conn.close()


def _run_postgres(url, report_only=False):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
                ("payment_allocation", "delinked"))
            if cur.fetchone() is None:
                print(_ORDER_ERROR)
                return
        with conn.cursor() as cur:
            writes, skips = _plan(cur, "%s")
            if not report_only:
                _apply(cur, "%s", writes)
        if report_only:
            conn.rollback()
        else:
            conn.commit()
        _print_summary(writes, skips, report_only)
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
        description="Migration 032: heal the party-level PLE double count")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="Enumerate what the real run would change; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("Migration 032 "
          + ("report complete (no writes)." if args.report_only else "complete."))
