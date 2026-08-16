"""Migration 035: register `disposal_gain_loss` and retype the accounts that are one (M94).

The account-type registry carried 24 types and none of them was a disposal
class, so the chart we ship typed `4220 Gain on Asset Disposal` as `revenue` —
exactly like `4110 Sales Revenue` — and `5340 Loss on Asset Disposal` as
`expense`, exactly like `5220 Rent Expense`. M91 tightened dispose-asset's
gain/loss gate as far as that registry allowed (27 postable P&L leaves down to
19) and could go no further: with only `revenue` and `expense` to name, the gate
cannot tell a disposal gain from ordinary sales revenue. This registers the
missing type and retypes the accounts on live installs that already are one.

ONE type covers both sides deliberately. dispose-asset's root_type check already
separates the gain side from the loss side, a single combined "Gain/(Loss) on
Disposal" account is a legitimate chart, and `exchange_gain_loss` is the same
shape for the same reason (one type over `4230 Exchange Gain` and `5330 Exchange
Loss`). Full reasoning: planning/simlogs/m94_SIM_2026-08-12.md §1.

WHAT THIS DOES NOT DO: it does not touch `gl_entry`. Not one row. `gl_entry`
stores `account_id`, so a disposal already posted follows its account into the
new classification with no ledger write at all — and the house rule is cancel =
reverse, never edit. Nor does it move any money, rename any account, or change
any `root_type`: every report that classifies income and expense does so by
`root_type` plus the account tree, so the P&L, balance sheet, trial balance,
cash flow and profitability ratios all produce IDENTICAL numbers before and
after (measured, SIM §7). The only column written is `account.account_type`.

MATCHING IS BY ROLE, NEVER BY NUMBER. An account is retyped only when

  * it is a leaf (`is_group = 0`) — a group account cannot post, so retyping one
    would change nothing; and
  * its `root_type` is 'income' (gain side) or 'expense' (loss side); and
  * its `account_type` is the plain one for that side ('revenue' / 'expense') or
    NULL — an account already typed something else was a deliberate choice and is
    never touched; and
  * its NAME reads as a gain or loss on a disposal: lowercased it contains
    'disposal' AND at least one of 'gain', 'loss', 'profit'.

The gain/loss word is load-bearing, not decoration. Our own industry seeds ship
"Waste Disposal" (restaurant) and "Waste Oil Disposal" (automotive) as ordinary
`expense` accounts, and a waste hauler's "Disposal Fees Revenue" is ordinary
income; a rule keyed on 'disposal' alone would reclassify all three. The number
4220 / 5340 is NOT sufficient either: those sit in a generic other-income /
other-expense range, and an install that renumbered its disposal account and let
something else take 4220 would have that something else retyped. Numbers are
REPORTED as near-misses and never acted on.

Two more signals are reported and never acted on, for the same reason:

  * an account occupying 4220 / 5340 whose name does not read as a disposal
    account — surfaced so a renamed-beyond-recognition account is visible rather
    than guessed at;
  * an account that has actually carried a disposal gain/loss leg (`gl_entry`
    rows under voucher_type 'asset_disposal') but does not match the name rule.
    Usage is evidence for a human, not a licence: under the defect M61 fixed,
    EVERY disposal leg went to the category's depreciation account, and a
    general "Write Off" account is a legitimate one-off target. Retyping either
    would be a worse defect than the one being fixed.

The name rule is English-only. A chart in another language matches nothing and
lands in the "matched nothing" path, which is correct: the migration would rather
do nothing than guess at a language it cannot read. The remedy in every
unmatched case is `update-account --account-type disposal_gain_loss` (M94 makes
retyping a supported, guarded operation for exactly this reason).

An existing registry row is never overridden. If `disposal_gain_loss` is already
registered and active, the registration step is a no-op and the retyping still
runs. If it is registered and INACTIVE, an operator disabled it deliberately:
nothing is registered and nothing is retyped, because retyping accounts to a type
this install has switched off would leave dispose-asset refusing them.

Idempotent twice over: the registry insert is guarded, and a second run finds
zero candidates (the accounts now carry `disposal_gain_loss`, which is not in the
candidate set) and says so as "already typed, left alone" rather than as an
account it declined to touch, on top of the runner's own ledger skip. Crash-safe: registration
and every retype are one transaction on one connection, so a crash rolls back to
the pre-run state and the runner's "fix the failing migration and re-run"
instruction is safe to follow.

`--report-only` writes nothing at all and states exactly what the real run would
do, per account, with its before and after type.

AUDIT TRAIL (M102). Every account this run retypes gets an `audit_log` row naming
it, its old type and its new one, plus one row for the type registration —
written on THIS connection inside the SAME transaction as the retype, so there is
never a row for a change that rolled back and never a committed change without
its row. `--report-only` writes none, and a second run retypes nothing and so
writes nothing: the trail cannot duplicate. Read it back with

    get-audit-log --audit-action "migration:035_disposal_gain_loss_account_type"

which is what makes the reversal in the paragraph above possible after the
terminal output is gone: the operator no longer has to remember which accounts
moved. Convention + gate: planning/simlogs/m102_SIM_2026-08-12.md.

THE ONE SKEW THIS RUN CREATES, AND WHY IT IS ANNOUNCED HERE. dispose-asset's
gain/loss gate lives in erpclaw-ops, an ADDON, and module_manager runs foundation
migrations only on the update-foundation path. So an install can hold an
erpclaw-ops that predates M94 while this migration retypes its 4220: that old
gate's allowlist is ('revenue','expense') and it will REFUSE the very account
this migration just designated. The stale code is the code that would have to
know, so the only place the operator can be told is HERE, on the run that creates
the skew. Every path that retypes an account (or finds one already retyped, which
is the same skew on a re-run) therefore prints the erpclaw-ops version the gate
needs, what this install's module catalog actually records, the update command,
and the undo. SIM §2 calls this mitigation 1; _print_ops_requirement is it.

Authored through the seam (ADR-0034): `erpclaw_lib.db.get_connection` for the
connection, `erpclaw_lib.seam.table_exists` for the catalog question. No raw DDL,
no connection-setting statements, no catalog table read by hand, so it works
unchanged on SQLite and PostgreSQL. Every statement is a FIXED string (migration
031's rule): no table name, column name or value is ever formatted into SQL.

SIM: planning/simlogs/m94_SIM_2026-08-12.md
Plan home: planning/pending_items.md row M94.

Usage:
    python3 035_disposal_gain_loss_account_type.py [--db-path PATH] [--report-only]
"""
import argparse
import importlib.util
import os
import re
import sys
from datetime import datetime, timezone

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.audit import audit_migration, migration_action  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

# M102: derived from the filename, never typed, so the audit trail's action
# string cannot drift from the stem migration_runner ledgers this file under.
MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]

# This migration changes values in a column that existed before it ran, on rows
# selected from the install's own chart, so it writes an audit trail (M102 §3).
MIGRATION_DATA_CLASS = "rows"

NEW_TYPE = "disposal_gain_loss"
_OWNER_SKILL = "erpclaw-assets"
_LABEL = "Disposal Gain/Loss"

# The addon that owns the gate, and the release of it that carries M94's tightened
# gain/loss allowlist. Anything older allows ('revenue','expense') and REFUSES an
# account typed NEW_TYPE, which is precisely the account this migration creates.
# _OPS_GATE_VERSION is pinned to erpclaw-ops' own SKILL.md by
# test_the_printed_ops_requirement_matches_the_version_erpclaw_ops_declares, so
# the number here cannot drift away from the module it names.
_OPS_MODULE = "erpclaw-ops"
_OPS_GATE_VERSION = "2.3.0"
_OPS_UPDATE_COMMAND = ("module_manager.py --action update-module --module-name "
                       + _OPS_MODULE)

# Per side: the root_type it must sit on, the account_type(s) that mean "typed
# plainly, i.e. the mistyping M94 is about", and the shipped chart's number. The
# number is used for REPORTING only (see _near_misses).
_SIDES = {
    "gain": {"root_type": "income", "plain": ("revenue",), "shipped_number": "4220"},
    "loss": {"root_type": "expense", "plain": ("expense",), "shipped_number": "5340"},
}

# The name predicate. Both halves are required: 'disposal' alone matches waste
# disposal expense and disposal-fee revenue, which are ordinary operating
# accounts and not this.
_DISPOSAL_WORD = "disposal"
_GAIN_LOSS_WORDS = ("gain", "loss", "profit")

# Fixed statements. Nothing is interpolated into any of them.
_SELECT_REGISTRY = ("SELECT account_type, is_active FROM account_type_registry "
                    "WHERE account_type = ?")
_INSERT_REGISTRY = ("INSERT INTO account_type_registry "
                    "(account_type, skill_name, label, is_active) VALUES (?, ?, ?, 1)")
_SELECT_LEAF_PL_ACCOUNTS = (
    "SELECT id, name, account_number, company_id, root_type, account_type "
    "FROM account WHERE is_group = 0 AND root_type IN ('income', 'expense')")
_UPDATE_ACCOUNT_TYPE = "UPDATE account SET account_type = ?, updated_at = ? WHERE id = ?"
_SELECT_DISPOSAL_LEGS = (
    "SELECT DISTINCT account_id FROM gl_entry "
    "WHERE voucher_type = 'asset_disposal' AND is_cancelled = 0")
# erpclaw_module belongs to erpclaw-modules and is only ever READ here; any module
# may read any table, and this is the install's own record of what it is running.
_SELECT_OPS_MODULE = ("SELECT version, install_status FROM erpclaw_module "
                      "WHERE name = ?")
_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reads_as_disposal_gain_loss(name):
    """True when an account NAME reads as a gain or loss on a disposal.

    Kept a module-level pure function so the rule can be exercised directly,
    without a database, by the tests that plant its false positives.
    """
    lowered = (name or "").lower()
    return (_DISPOSAL_WORD in lowered
            and any(word in lowered for word in _GAIN_LOSS_WORDS))


def _side_of(row):
    """Which disposal side this account could be, or None if it is not a candidate.

    A row qualifies on root_type AND on carrying the plain type for that side (or
    no type at all). Anything already typed something specific — cost_of_goods_sold,
    rounding, exchange_gain_loss, depreciation, and disposal_gain_loss itself on a
    second run — falls out here.
    """
    for side, spec in _SIDES.items():
        if row["root_type"] != spec["root_type"]:
            continue
        if row["account_type"] in spec["plain"] or row["account_type"] is None:
            return side
    return None


def _classify(rows, disposal_leg_account_ids):
    """Split every leaf P&L account into: retype / already / near-miss / used-unmatched.

    `already` is not a formality. On a second run — and on any install where an
    operator typed the account by hand — the disposal account carries NEW_TYPE, so
    it is no longer a candidate, and without this bucket it falls through to the
    two "not retyped" reports and gets described as an account whose name does not
    read as a disposal account. That is false, and it is the sentence an operator
    would read on the ONE run where everything went right.

    Pure, so the tests can plant an offender and read the decision without a
    migration run.
    """
    retype, already, near_miss, used_unmatched = [], [], [], []
    shipped_numbers = {spec["shipped_number"]: side for side, spec in _SIDES.items()}
    for row in rows:
        if row["account_type"] == NEW_TYPE:
            already.append(row)
            continue
        side = _side_of(row)
        if side and reads_as_disposal_gain_loss(row["name"]):
            retype.append((side, row))
            continue
        number = (row["account_number"] or "").strip()
        # A near-miss is only interesting when the account could otherwise have
        # been a candidate. An account sitting on 4220 typed exchange_gain_loss
        # was deliberately typed as other machinery, and reporting it is noise.
        if side is not None and shipped_numbers.get(number) == side:
            near_miss.append(row)
        elif row["id"] in disposal_leg_account_ids:
            used_unmatched.append(row)
    return retype, already, near_miss, used_unmatched


def _registry_state(conn):
    """(registered, active) for NEW_TYPE. A missing registry reads as unregistered."""
    row = conn.execute(_SELECT_REGISTRY, (NEW_TYPE,)).fetchone()
    if row is None:
        return False, False
    return True, bool(row["is_active"])


def _disposal_leg_account_ids(conn, db_path):
    """Accounts that have carried a disposal gain/loss leg. Reporting only.

    `gl_entry` is foundation and always present; the guard is for the
    minimal-install case where it somehow is not, which must be a clean empty
    set rather than a migration failure.
    """
    if not seam.table_exists("gl_entry", db_path):
        return set()
    return {r[0] for r in conn.execute(_SELECT_DISPOSAL_LEGS).fetchall()}


def _describe(row):
    number = (row["account_number"] or "").strip() or "(no number)"
    return "%s / %s [%s] company=%s" % (
        number, row["name"], row["account_type"] or "untyped", row["company_id"])


def version_tuple(text):
    """(major, minor, patch) from a version string, or None when it is not one.

    Deliberately lenient about a leading 'v' and a missing patch, and deliberately
    refuses anything it cannot read rather than guessing a number: an
    uncomparable version has to print as uncomparable, because the alternative is
    a confident "you are fine" over a version nobody parsed.
    """
    match = _VERSION_RE.match(text or "")
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _installed_ops(conn, db_path):
    """(version, install_status) for erpclaw-ops from the module catalog.

    (None, None) when the catalog table is absent or carries no row: on this
    install nothing enforces the disposal gate today, which is a different
    sentence from "your erpclaw-ops is stale" and is printed as one.
    """
    if not seam.table_exists("erpclaw_module", db_path):
        return None, None
    row = conn.execute(_SELECT_OPS_MODULE, (_OPS_MODULE,)).fetchone()
    if row is None:
        return None, None
    return row["version"], row["install_status"]


def _print_ops_requirement(conn, db_path, would):
    """Announce the erpclaw-ops version this retyping requires (SIM §2, mitigation 1).

    This is the one direction the M94 compatibility design cannot absorb from the
    assets side. After this migration 4220 carries NEW_TYPE; an erpclaw-ops that
    predates the M94 gate allows only ('revenue','expense') and so refuses it. The
    stale code is the code that would have to know, which leaves exactly one place
    the operator can be told: the run that creates the skew.

    `would` is True on --report-only, so the tense matches what actually happened.
    """
    print("  the account(s) above %s typed '%s'. dispose-asset accepts that type "
          "only from %s >= %s (the release carrying the M94 disposal gate); an "
          "older one allows 'revenue'/'expense' and REFUSES it."
          % ("would be" if would else "are", NEW_TYPE, _OPS_MODULE,
             _OPS_GATE_VERSION))
    version, status = _installed_ops(conn, db_path)
    if version is None:
        print("  %s is not installed here (no module-catalog row), so nothing "
              "enforces the disposal gate on this install today; the requirement "
              "applies from the moment it is installed." % _OPS_MODULE)
        return
    print("  this install's module catalog records %s %s (install_status %s)."
          % (_OPS_MODULE, version, status))
    installed = version_tuple(version)
    required = version_tuple(_OPS_GATE_VERSION)
    if installed is not None and installed >= required:
        print("  that meets the requirement; nothing further to do for %s."
              % _OPS_MODULE)
        return
    if installed is None:
        print("  that version string cannot be compared with %s, so check it by "
              "hand before relying on the gate." % _OPS_GATE_VERSION)
    else:
        print("  that is OLDER than %s, so dispose-asset will REFUSE the "
              "account(s) above until it is updated." % _OPS_GATE_VERSION)
    print("  update it with:  %s" % _OPS_UPDATE_COMMAND)
    print("  or put an account back with:  update-account --account-id <id> "
          "--account-type revenue --reclassify-posted   (use 'expense' on the "
          "loss side)")


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    conn = get_connection(path)
    try:
        if not seam.table_exists("account_type_registry", path):
            print("  account_type_registry absent (pre-M0 install). Nothing to do.")
            return {"registered": False, "retyped": [], "already": [],
                    "near_misses": [], "used_unmatched": [], "audit_rows": 0,
                    "report_only": report_only,
                    "reason": "no account_type_registry"}

        registered, active = _registry_state(conn)
        if registered and not active:
            # An operator switched this type off. Retyping accounts to a type
            # this install has disabled would leave dispose-asset refusing them,
            # so respect the decision and change nothing.
            print("  account_type '%s' is registered but INACTIVE on this install; "
                  "an operator disabled it deliberately. Nothing registered, "
                  "nothing retyped." % NEW_TYPE)
            return {"registered": True, "retyped": [], "already": [],
                    "near_misses": [], "used_unmatched": [], "audit_rows": 0,
                    "report_only": report_only,
                    "reason": "type deactivated by operator"}

        rows = conn.execute(_SELECT_LEAF_PL_ACCOUNTS).fetchall()
        legs = _disposal_leg_account_ids(conn, path)
        retype, already, near_miss, used_unmatched = _classify(rows, legs)

        print("  account_type '%s': %s" % (
            NEW_TYPE, "already registered" if registered else "will be registered"))
        print("  leaf income/expense accounts scanned: %d" % len(rows))
        for row in already:
            print("  already typed '%s', left alone: %s" % (NEW_TYPE, _describe(row)))

        for row in near_miss:
            print("  NOT retyped (occupies the shipped chart's disposal number but "
                  "its name does not read as a disposal gain/loss account): %s"
                  % _describe(row))
        for row in used_unmatched:
            print("  NOT retyped (has carried a disposal gain/loss leg but its name "
                  "does not read as a disposal gain/loss account): %s" % _describe(row))
        if near_miss or used_unmatched:
            print("  ^ retype any of the above by hand with: update-account "
                  "--account-id <id> --account-type %s" % NEW_TYPE)

        # More than one match on one side within one company is legal and is
        # retyped, but it usually means a duplicate account, so say so.
        per_company_side = {}
        for side, row in retype:
            per_company_side.setdefault((row["company_id"], side), []).append(row)
        for (company_id, side), group in sorted(per_company_side.items()):
            if len(group) > 1:
                print("  NOTE: company %s has %d accounts matching the %s side: %s"
                      % (company_id, len(group), side,
                         ", ".join(r["name"] for r in group)))

        if report_only:
            if retype:
                for side, row in retype:
                    print("  report-only: would retype %s -> '%s' (%s side)"
                          % (_describe(row), NEW_TYPE, side))
            elif already:
                print("  report-only: every disposal account already carries '%s'; "
                      "nothing would be retyped" % NEW_TYPE)
            else:
                print("  report-only: no account matches the disposal gain/loss rule; "
                      "nothing would be retyped")
            if retype or already:
                _print_ops_requirement(conn, path, would=True)
            print("  report-only: would %s the account type"
                  % ("leave registered" if registered else "register"))
            print("  report-only: gl_entry is NOT touched, on this or the real run")
            print("  report-only: no audit_log row is written either — a trail for "
                  "a change that did not happen would be the lie M102 exists to "
                  "prevent. The real run writes %d."
                  % (len(retype) + (0 if registered else 1)))
            return {"registered": registered,
                    "retyped": [r["id"] for _, r in retype],
                    "already": [r["id"] for r in already],
                    "near_misses": [r["id"] for r in near_miss],
                    "used_unmatched": [r["id"] for r in used_unmatched],
                    "audit_rows": 0,
                    "report_only": True}

        # One transaction: the registration and every retype commit together or
        # not at all, so a crash can never leave accounts carrying a type this
        # install does not know.
        audit_rows = 0
        if not registered:
            conn.execute(_INSERT_REGISTRY, (NEW_TYPE, _OWNER_SKILL, _LABEL))
            audit_migration(
                conn, MIGRATION_ID, "account_type_registry", NEW_TYPE,
                new_values={"account_type": NEW_TYPE, "skill_name": _OWNER_SKILL,
                            "label": _LABEL, "is_active": 1},
                description="migration %s registered account type '%s'"
                            % (MIGRATION_ID, NEW_TYPE))
            audit_rows += 1
            print("  registered account_type '%s' (%s, owner %s)"
                  % (NEW_TYPE, _LABEL, _OWNER_SKILL))

        stamp = _now()
        for side, row in retype:
            conn.execute(_UPDATE_ACCOUNT_TYPE, (NEW_TYPE, stamp, row["id"]))
            # M102 — same connection, same transaction as the UPDATE above. The
            # old type is read from the row this run classified, so the trail
            # and the change describe one decision, not two.
            audit_migration(
                conn, MIGRATION_ID, "account", row["id"],
                old_values={"account_type": row["account_type"]},
                new_values={"account_type": NEW_TYPE},
                description="migration %s retyped %s (%s side); reverse with "
                            "update-account --account-id %s --account-type %s "
                            "--reclassify-posted"
                            % (MIGRATION_ID, _describe(row), side, row["id"],
                               row["account_type"] or "revenue"))
            audit_rows += 1
            print("  retyped %s -> '%s' (%s side)" % (_describe(row), NEW_TYPE, side))
        conn.commit()
        if audit_rows:
            print("  audit trail: %d audit_log row(s), committed with the change. "
                  "Read them back with:  get-audit-log --audit-action \"%s\""
                  % (audit_rows, migration_action(MIGRATION_ID)))

        if not retype and already:
            print("  every disposal account already carries '%s'; nothing to retype."
                  % NEW_TYPE)
        elif not retype:
            # Only when this install has NO disposal-typed account at all, so the
            # advice is not handed to someone whose migration went fine.
            print("  no account matches the disposal gain/loss rule; nothing retyped. "
                  "Create one with add-account --account-type %s, or retype an "
                  "existing account with update-account --account-type %s."
                  % (NEW_TYPE, NEW_TYPE))
        if retype or already:
            _print_ops_requirement(conn, path, would=False)
        print("  gl_entry was not read for a write and not written to; the "
              "disposals already posted follow their accounts.")
        return {"registered": True,
                "retyped": [r["id"] for _, r in retype],
                "already": [r["id"] for r in already],
                "near_misses": [r["id"] for r in near_miss],
                "used_unmatched": [r["id"] for r in used_unmatched],
                "audit_rows": audit_rows,
                "report_only": False}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 035: register disposal_gain_loss and retype the "
                    "accounts that are one")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would do; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 035 "
          + ("report complete (no writes)." if args.report_only else "complete."))
