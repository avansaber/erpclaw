"""Migration 051: backfill the payment-ledger row for approved expense claims.

From this release, approving an expense claim writes one payment-ledger row,
so what the business owes the employee reads correctly until the claim is
paid. Claims approved on an earlier release carry no such row: on an upgraded
install they read as owed nothing, and an employee with both an old and a new
claim reads less than the general ledger says.

This migration writes the missing row for every claim that is still
``approved``, exactly as approval now writes it: one row under the claim's
own voucher, against the claim, for the claim total on the payable account
the claim's own approval ledger credited to the employee, dated to the
expense date, carrying the claim company's default currency as approval now
writes it. The remark carries the backfill marker so the row stays
distinguishable from one approval wrote.

PROVE THE ACCOUNT, NEVER GUESS IT. For each claim lacking its own live row,
the migration reads the claim's own live approval ledger credits (party
employee, credit above zero). It writes only when those rows name exactly one
distinct account, that account exists and belongs to the claim's company,
and their credits sum to the claim total exactly. Anything else is SKIPPED
AND REPORTED with its reason: no approval ledger credit, approval credits
more than one account, a credited account that does not exist, a credited
account that belongs to another company, or an approval credit that does not
equal the claim total. The row stays missing and the operator repairs the
approval ledger by hand. A skipped claim keeps INV-27 red for its employee
until that repair lands.

IDEMPOTENT. A claim that already carries its own live row is listed under
already-present and untouched; a second run writes nothing and no audit row.
Claims in any other status are never touched.

REPORT-ONLY IS THE FIRST-CLASS PATH. ``--report-only`` (or
``report_only=True``) prints the plan and the skips, writes neither rows nor
audit rows, and leaves the database exactly as it found it. The report-only
result carries the plan under ``planned`` while ``written`` stays empty; a
real run carries the same rows under both keys.

AUDIT TRAIL (M102). Every claim this run backfills gets ONE ``audit_log`` row
naming the appended row's id, account, amount and currency, at the claim's own grain,
on the SAME connection inside the SAME transaction as the insert, so a
report run or a crashed run leaves no trail and a committed backfill always
has one. Read it back with ``get-system-audit-log`` for the action
``migration:051_backfill_expense_claim_ple``.
"""
import argparse
import importlib.util
import os
import sys
import uuid
from decimal import Decimal

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.audit import audit_migration  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

# Derived, never typed: the runner ledgers this file under its stem, and
# `get-system-audit-log --audit-action migration:<stem>` has to match that exact string.
MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]

# M102: this appends one row per proven claim whose amount comes from this
# install's own claim totals, and skips what it cannot prove. That is the
# definition of data-changing, so it writes the trail.
MIGRATION_DATA_CLASS = "rows"

MIGRATION_DATA_EXEMPTIONS = {}

# Fixed statements, spelled out in full (migration 031's rule): no name is ever
# formatted INTO a statement, because an f-string that assembles SQL is
# indistinguishable from an injection site to the Article-10 scanner.
_SELECT_APPROVED_CLAIMS = (
    "SELECT id, naming_series, employee_id, expense_date, total_amount, company_id "
    "FROM expense_claim WHERE status = 'approved' ORDER BY expense_date, id")
_SELECT_OWN_ROW = (
    "SELECT id FROM payment_ledger_entry "
    "WHERE voucher_type = 'expense_claim' AND voucher_id = ? AND delinked = 0")
_SELECT_APPROVAL_CREDITS = (
    "SELECT account_id, credit FROM gl_entry "
    "WHERE voucher_type = 'expense_claim' AND voucher_id = ? AND is_cancelled = 0 "
    "AND party_type = 'employee' AND party_id = ?")
_SELECT_ACCOUNT_COMPANY = ("SELECT company_id FROM account WHERE id = ?")
_SELECT_COMPANY_CURRENCY = ("SELECT default_currency FROM company WHERE id = ?")


def _two(value):
    """Format a money value as an exact two-place string. Never float."""
    return "%s" % Decimal(str(value)).quantize(Decimal("0.01"))


def _plan(conn):
    """Compute, read-only, exactly what the real run would append.

    Returns (writes, already_present, skips). ``--report-only`` prints this
    and stops; the real run prints the same thing and applies it, so the two
    can never describe different work.
    """
    claims = conn.execute(_SELECT_APPROVED_CLAIMS).fetchall()
    writes, already_present, skips = [], [], []
    for claim in claims:
        claim_id = claim["id"]
        naming_series = claim["naming_series"]
        if conn.execute(_SELECT_OWN_ROW, (claim_id,)).fetchone() is not None:
            already_present.append({"expense_claim_id": claim_id,
                                    "naming_series": naming_series})
            continue
        total = Decimal(str(claim["total_amount"]))
        credits = [row for row in conn.execute(
            _SELECT_APPROVAL_CREDITS,
            (claim_id, claim["employee_id"])).fetchall()
            if Decimal(str(row["credit"] or "0")) > 0]
        if not credits:
            skips.append({"expense_claim_id": claim_id,
                          "naming_series": naming_series,
                          "reason": "no approval ledger credit"})
            continue
        accounts = {row["account_id"] for row in credits}
        if len(accounts) != 1:
            skips.append({"expense_claim_id": claim_id,
                          "naming_series": naming_series,
                          "reason": "approval credits more than one account"})
            continue
        account_id = next(iter(accounts))
        account = conn.execute(_SELECT_ACCOUNT_COMPANY, (account_id,)).fetchone()
        if account is None:
            skips.append({
                "expense_claim_id": claim_id,
                "naming_series": naming_series,
                "reason": ("approval credit account %s does not exist"
                           % account_id)})
            continue
        if account["company_id"] != claim["company_id"]:
            skips.append({
                "expense_claim_id": claim_id,
                "naming_series": naming_series,
                "reason": ("approval credit account %s belongs to another company"
                           % account_id)})
            continue
        credit_sum = sum((Decimal(str(row["credit"])) for row in credits),
                         Decimal("0"))
        if credit_sum != total:
            skips.append({
                "expense_claim_id": claim_id,
                "naming_series": naming_series,
                "reason": ("approval credit %s does not equal claim total %s"
                           % (_two(credit_sum), _two(total)))})
            continue
        currency_row = conn.execute(_SELECT_COMPANY_CURRENCY, (claim["company_id"],)).fetchone()
        currency = currency_row["default_currency"] if currency_row and currency_row["default_currency"] else "USD"
        writes.append({"expense_claim_id": claim_id,
                       "naming_series": naming_series,
                       "employee_id": claim["employee_id"],
                       "company_id": claim["company_id"],
                       "expense_date": claim["expense_date"],
                       "account_id": account_id,
                       "amount": _two(total),
                       "currency": currency})
    return writes, already_present, skips


def _apply(conn, writes):
    """Append one payment-ledger row per proven claim, with its audit row.

    The row copies the shape ``approve-expense-claim`` writes (same columns,
    same voucher and against voucher, same dating), with the backfill marker
    in remarks. The row carries the claim company's default currency, as
    approval now writes it. One audit row per claim rides the same
    connection in the same transaction as the insert.
    """
    for item in writes:
        ple_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO payment_ledger_entry (id, posting_date, account_id, "
            "party_type, party_id, voucher_type, voucher_id, "
            "against_voucher_type, against_voucher_id, amount, "
            "amount_in_account_currency, currency, remarks) "
            "VALUES (?, ?, ?, 'employee', ?, 'expense_claim', ?, "
            "'expense_claim', ?, ?, ?, ?, ?)",
            (ple_id, item["expense_date"], item["account_id"],
             item["employee_id"], item["expense_claim_id"],
             item["expense_claim_id"], item["amount"], item["amount"],
             item["currency"],
             "Expense claim %s approved (backfilled by migration 051)"
             % item["naming_series"]))
        # M102 — same connection, same transaction as the INSERT above. The
        # values name the row that drove the write, so the trail and the row
        # cannot disagree, and the appended row id is named for a reversal.
        audit_migration(
            conn, MIGRATION_ID, "expense_claim", item["expense_claim_id"],
            new_values={"payment_ledger_entry_id": ple_id,
                        "account_id": item["account_id"],
                        "amount": item["amount"],
                        "currency": item["currency"]},
            description=("migration 051 backfilled the missing payment-ledger "
                         "row %s (+%s on %s) for approved expense claim %s"
                         % (ple_id, item["amount"], item["account_id"],
                            item["naming_series"])))
        item["payment_ledger_entry_id"] = ple_id


def _print_summary(writes, already_present, skips, report_only):
    verb = "would append" if report_only else "appended"
    print("  expense-claim PLE backfill: %s %d missing row(s)."
          % (verb, len(writes)))
    for item in writes:
        # Amounts and accounts are printed, not just counted: the report is the
        # operator's review artifact before the real run, and a count cannot be
        # checked against the ledger.
        print("    claim %s (%s): %s on account %s in %s"
              % (item["expense_claim_id"], item["naming_series"],
                 item["amount"], item["account_id"], item["currency"]))
    print("  already present: %d claim(s) already carry their own live row "
          "-- untouched." % len(already_present))
    # Skips are REPORTED, never silent.
    print("  skipped: %d claim(s) this migration refuses to backfill."
          % len(skips))
    for skip in skips:
        print("    claim %s (%s): %s"
              % (skip["expense_claim_id"], skip["naming_series"],
                 skip["reason"]))
    if skips:
        print("  a skipped claim keeps INV-27 red for its employee until an "
              "operator repairs the approval ledger.")
    if writes and not report_only:
        print("  audit trail: %d audit_log row(s), committed with the "
              "backfill. Read them back with:  get-system-audit-log --audit-action "
              "\"migration:051_backfill_expense_claim_ple\"" % len(writes))
    elif writes:
        print("  report-only: no audit_log row is written -- a trail for a change "
              "that did not happen would be the lie M102 exists to prevent. The "
              "real run writes %d." % len(writes))


def run_migration(db_path=None, report_only=False):
    for table in ("expense_claim", "payment_ledger_entry", "gl_entry",
                  "account"):
        if not seam.table_exists(table, db_path):
            print("  %s absent on this install. Nothing to migrate." % table)
            return {"migration": MIGRATION_ID, "report_only": report_only,
                    "planned": [], "written": [], "already_present": [], "skipped": []}
    conn = get_connection(db_path)
    try:
        writes, already_present, skips = _plan(conn)
        if not report_only:
            _apply(conn, writes)
            conn.commit()
    finally:
        conn.close()
    _print_summary(writes, already_present, skips, report_only)
    if report_only:
        return {"migration": MIGRATION_ID, "report_only": report_only,
                "planned": writes, "written": [], "already_present": already_present,
                "skipped": skips}
    return {"migration": MIGRATION_ID, "report_only": report_only,
            "planned": writes, "written": writes, "already_present": already_present,
            "skipped": skips}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--report-only", action="store_true",
                        help="Enumerate what the real run would backfill; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("Migration 051 "
          + ("report complete (no writes)." if args.report_only else "complete."))
