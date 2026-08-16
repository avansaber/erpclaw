"""Migration 036: link every elimination entry to the transaction it came from (M95).

`generate-elimination-entries` was not idempotent. Re-running it for the same
consolidation group and period returned `entries_created: 1` every time and left a
duplicate `advacct_elimination_entry` row, which the consolidated trial balance then
double-counted (measured on the real flow: 50,000.00 of eliminations against a single
25,000.00 intercompany sale). The engine that flow REPLACED did skip when an entry
already existed, so the replacement was weaker than the thing it replaced — on exactly
the behaviour of a report anyone reruns. M63-C's retirement steer sends every user of the
old flow here by name, so the duplicate became reachable by following our own
instructions.

The fix needs one fact the table never recorded: which intercompany transaction an
elimination row was derived from. A pre-M95 row states only type/from/to inside its
`description`, and two legitimately distinct transactions of the same shape produce
byte-identical rows — so nothing content-derived can serve as an identity. This migration
adds that fact to installs that already exist:

  1. ADD COLUMN `advacct_elimination_entry.source_ic_transaction_id` (nullable).
  2. BACKFILL it, per (group, period), by pairing each unlinked `ic_elimination` row with
     a posted intercompany transaction matching the type/from/to parsed out of its
     description AND its amount, one transaction per row, in a deterministic order.
  3. CREATE the partial unique index `uq_advacct_ee_source`, the object `init_schema.py`
     now creates on a fresh install.

WHAT "THE SAME OBJECT" IS AND IS NOT, MEASURED RATHER THAN ASSERTED. An earlier version
of this docstring claimed a migrated database and a fresh one hold the same schema
"rather than nearly". They were then compared (fresh install vs install-rewind-migrate,
SQLite), and that claim was too strong:

  * the same — the column SET, and every column's type and nullability; the primary key;
    the index name set; and the index where it counts: UNIQUE, over
    (group_id, period_date, source_ic_transaction_id) in that order, with the partial
    predicate `source_ic_transaction_id IS NOT NULL`;
  * NOT the same — the column ORDER and the index DDL TEXT. `init_schema` declares the
    column mid-table (ordinal 9); ALTER TABLE ADD COLUMN can only append, so a migrated
    table carries it last (ordinal 11). The index text differs by whitespace only:
    init_schema's heredoc keeps its newlines, this file's constant is one line.
    `seam.describe_table` compares its column list IN ORDER, so it reports the two
    databases as different — that difference is this one, and it is the whole of it.

The ordinal is left alone deliberately. Nothing reads this table positionally (the one
`SELECT *`, in the consolidation trial balance, is consumed through `row_to_dict`), so it
surfaces only as JSON key order — and matching it would mean rebuilding the table
(create, copy, drop, rename) under an operator's consolidation numbers to move a column
no statement names by position. The inaccuracy is corrected here rather than in the
schema.

WHAT THIS DOES NOT DO: it does not delete a single row. An install that ran the defect
holds surplus elimination rows, and after this migration they are still there, still
NULL, and every one of them is NAMED in the output. They are an operator's data and
removing them changes their consolidated numbers; the M63-C QA bounce is the standing
lesson about a migration that destroys what it does not fully understand. Generation is
idempotent from this point forward, which is what the row asks for.

Nor does it touch `gl_entry`. It cannot: these entries never reach the ledger
(`init_schema.py`'s own note — group elimination lives in the consolidation layer, never
in `gl_entry`), and the accounting-adv module contains no `gl_entry` write at all.

MATCHING IS EXACT AND DELIBERATELY NARROW. A row is linked only when

  * its description parses as the generator's own sentence
    ("Elimination: <type> from <from-company> to <to-company>"); and
  * a transaction exists with that from/to/type, that exact amount TEXT-for-TEXT, and
    ic_status 'posted' (the only status the generator ever eliminated); and
  * that transaction has not already been claimed by another row in the same group and
    period.

Anything else is REPORTED and left NULL: an unparseable description, a row with no
unclaimed match (the duplicate residue), a `currency_translation` row (authored by hand,
derived from nothing, and correctly outside the constraint forever). Guessing a source
would attach a real transaction to the wrong row and make the next generation skip work
it should do — worse than the defect being fixed.

Group membership is deliberately NOT re-checked. The from/to companies come out of the
row itself, and an entity deactivated since the entry was generated would turn a correct
backfill into a false negative.

Idempotent: a second run finds no unlinked row it can match, the column present and the
index present, and says so.

CRASH SAFETY, STATED AS MEASURED RATHER THAN ASSUMED. Two phases, not one:

  phase 1 — ADD COLUMN, committed on its own;
  phase 2 — every link plus the index, one transaction on one connection.

Phase 1 cannot be folded into phase 2 on SQLite: with the driver's legacy transaction
control a DDL statement issued while no transaction is open self-commits (measured on
SQLite 3.53.2 / Python 3.14 — an ALTER survived an immediate rollback), so a docstring
claiming one atomic step would be false on the default backend. The split is therefore
made explicit instead of accidental, and it is safe because a bare nullable column is
inert: no pre-M95 code reads it, and no row claims a source until phase 2 says so. A
crash between the phases leaves exactly that inert column, and the runner's "fix it and
re-run" instruction finishes the job. A crash INSIDE phase 2 rolls back every link, so
the unique index can never be created over a half-linked table.

`--report-only` writes nothing at all — not even a rolled-back ALTER, which would still
take the table lock (migration 031's rule) — and states exactly what the real run would
link and what it would leave alone.

Authored through the seam (ADR-0034): `erpclaw_lib.db.get_connection` for the connection,
`erpclaw_lib.seam.table_exists` / `column_names` / `index_names` for the catalog
questions. No raw driver call, no connection setting, no catalog table read by hand, so
it runs unchanged on SQLite and PostgreSQL. Every statement is a FIXED string (migration
031's rule): no table name, column name or value is ever formatted into SQL.

SIM: planning/simlogs/m95_SIM_2026-08-12.md
Plan home: planning/pending_items.md row M95.

Usage:
    python3 036_elimination_entry_source_link.py [--db-path PATH] [--report-only]
"""
import argparse
import importlib.util
import os
import re
import sys

# M102: adds a column and backfills only that column. Before this run
# `source_ic_transaction_id` held nothing on any install, so nothing a row held
# before the migration is different afterwards — the backfill-a-column-this-
# migration-added case, not a rewrite. The surplus rows it cannot link are left
# exactly as they were (see M114); it deletes nothing and rewrites nothing.
# Declared at the merge that brought M95 and M102 together: M95 was authored
# before the convention existed, and M102's gate caught it on its first day.
MIGRATION_DATA_CLASS = "none"

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

TABLE = "advacct_elimination_entry"
COLUMN = "source_ic_transaction_id"
INDEX = "uq_advacct_ee_source"

# Fixed statements, spelled out in full (migration 031's rule). The constants above
# name the same objects for the seam calls and the printed messages, but no name is
# ever formatted INTO a statement: an f-string that assembles SQL is indistinguishable
# from an injection site to the Article-10 scanner, and being distinguishable is the
# point.
_ADD_COLUMN = ("ALTER TABLE advacct_elimination_entry "
               "ADD COLUMN source_ic_transaction_id TEXT "
               "REFERENCES advacct_ic_transaction(id)")
# Partial on purpose: a hand-authored currency-translation entry has no source and
# must stay repeatable. Same name, key columns and predicate as the index
# init_schema.py creates; the TEXT differs from it in whitespace only (docstring).
_CREATE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_advacct_ee_source "
    "ON advacct_elimination_entry(group_id, period_date, source_ic_transaction_id) "
    "WHERE source_ic_transaction_id IS NOT NULL")
_SELECT_UNLINKED = (
    "SELECT id, group_id, period_date, amount, description "
    "FROM advacct_elimination_entry "
    "WHERE entry_type = 'ic_elimination' AND source_ic_transaction_id IS NULL "
    "ORDER BY group_id, period_date, created_at, id")
# Pre-column form of the same query: on a database that has not been altered yet
# every ic_elimination row is unlinked by definition, and --report-only has to be
# able to enumerate them (that is the workflow this migration mandates).
_SELECT_ALL_IC = (
    "SELECT id, group_id, period_date, amount, description "
    "FROM advacct_elimination_entry "
    "WHERE entry_type = 'ic_elimination' "
    "ORDER BY group_id, period_date, created_at, id")
_SELECT_CLAIMED = (
    "SELECT group_id, period_date, source_ic_transaction_id "
    "FROM advacct_elimination_entry "
    "WHERE source_ic_transaction_id IS NOT NULL")
_SELECT_POSTED_IC = (
    "SELECT id, from_company_id, to_company_id, transaction_type, amount "
    "FROM advacct_ic_transaction WHERE ic_status = 'posted' "
    "ORDER BY created_at, id")
_LINK = ("UPDATE advacct_elimination_entry SET source_ic_transaction_id = ? "
         "WHERE id = ?")

# The generator's own sentence, and the only one this migration claims to read:
#   "Elimination: sale from <uuid> to <uuid>"
_DESCRIPTION_RE = re.compile(
    r"^Elimination:\s+(?P<type>\S+)\s+from\s+(?P<from>\S+)\s+to\s+(?P<to>\S+)\s*$")


def parse_source_shape(description):
    """(transaction_type, from_company_id, to_company_id) or None.

    Module-level and pure so the rule can be exercised directly, without a
    database, by the tests that plant descriptions it must refuse.
    """
    match = _DESCRIPTION_RE.match(description or "")
    if not match:
        return None
    return (match.group("type"), match.group("from"), match.group("to"))


def plan_links(rows, transactions, claimed):
    """Decide, without touching the database, what each unlinked row resolves to.

    Returns ``(links, unmatched)`` where ``links`` is [(row_id, transaction_id)] and
    ``unmatched`` is [(row, reason)]. Pure, so a test can read the decision rather
    than infer it from a run.

    `claimed` maps (group_id, period_date) -> {transaction_id, ...} already linked,
    and grows as rows are paired. It is scoped per group and period on purpose: two
    consolidation groups may legitimately eliminate the SAME transaction, each for
    itself, and a global claim set would starve the second one.
    """
    by_shape = {}
    for txn in transactions:
        key = (txn["transaction_type"], txn["from_company_id"],
               txn["to_company_id"], txn["amount"])
        by_shape.setdefault(key, []).append(txn["id"])

    links, unmatched = [], []
    for row in rows:
        shape = parse_source_shape(row["description"])
        if shape is None:
            unmatched.append((row, "description is not the generator's sentence"))
            continue
        key = (shape[0], shape[1], shape[2], row["amount"])
        scope = claimed.setdefault((row["group_id"], row["period_date"]), set())
        candidate = next((t for t in by_shape.get(key, []) if t not in scope), None)
        if candidate is None:
            unmatched.append((
                row,
                "no unclaimed posted transaction matches type/from/to/amount "
                "(a duplicate left by the pre-M95 defect, or a source since removed)"))
            continue
        scope.add(candidate)
        links.append((row["id"], candidate))
    return links, unmatched


def _rows_as_dicts(cursor_rows):
    """Driver rows -> plain dicts, so the pure planner never sees a cursor."""
    return [{"id": r["id"], "group_id": r["group_id"],
             "period_date": r["period_date"], "amount": r["amount"],
             "description": r["description"]} for r in cursor_rows]


def _transactions_as_dicts(cursor_rows):
    return [{"id": r["id"], "from_company_id": r["from_company_id"],
             "to_company_id": r["to_company_id"],
             "transaction_type": r["transaction_type"],
             "amount": r["amount"]} for r in cursor_rows]


def _existing_claims(conn, has_column):
    claimed = {}
    if not has_column:
        return claimed
    for row in conn.execute(_SELECT_CLAIMED).fetchall():
        claimed.setdefault((row["group_id"], row["period_date"]), set()).add(row[COLUMN])
    return claimed


def _describe(row):
    return "entry %s (group %s, period %s, amount %s)" % (
        row["id"], row["group_id"], row["period_date"], row["amount"])


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    conn = get_connection(path)
    try:
        if not seam.table_exists(TABLE, path):
            print(f"  {TABLE} absent on this install. Nothing to do.")
            return {"column_added": False, "links": [], "unmatched": [],
                    "index_created": False, "report_only": report_only,
                    "reason": "table absent"}

        has_column = COLUMN in seam.column_names(TABLE, path)
        has_index = INDEX in seam.index_names(TABLE, path)

        rows = _rows_as_dicts(conn.execute(
            _SELECT_UNLINKED if has_column else _SELECT_ALL_IC).fetchall())
        transactions = _transactions_as_dicts(
            conn.execute(_SELECT_POSTED_IC).fetchall())
        links, unmatched = plan_links(rows, transactions,
                                      _existing_claims(conn, has_column))

        print("  %s.%s: %s" % (TABLE, COLUMN,
                               "already present" if has_column
                               else ("would be added (ALTER TABLE ... ADD COLUMN)"
                                     if report_only else "will be added")))
        print("  unlinked ic_elimination rows: %d" % len(rows))
        print("  posted intercompany transactions available to match: %d"
              % len(transactions))

        # Every decision is printed per row, never as a bare count: this output is
        # the operator's review artifact before the real run, and a count cannot be
        # checked against the books.
        for row_id, txn_id in links:
            row = next(r for r in rows if r["id"] == row_id)
            print("  %s %s -> source %s" % (
                "would link" if report_only else "linked", _describe(row), txn_id))
        for row, reason in unmatched:
            print("  NOT linked (%s): %s" % (reason, _describe(row)))
        if unmatched:
            print("  ^ left exactly as they are. Nothing is deleted here; a surplus "
                  "row from the pre-M95 defect is a consolidation number somebody "
                  "may have relied on, so removing one is a decision for whoever "
                  "keeps the books.")

        if report_only:
            print("  report-only: index %s %s" % (
                INDEX, "already present" if has_index else "would be created"))
            print("  report-only: nothing was written; gl_entry is not touched on "
                  "this or the real run")
            return {"column_added": False, "links": links,
                    "unmatched": [r["id"] for r, _ in unmatched],
                    "index_created": False, "report_only": True}

        # Phase 1: the column, on its own. See the module docstring — SQLite's
        # legacy transaction control self-commits a DDL statement issued outside a
        # transaction, so this commit is written down rather than left implicit.
        if not has_column:
            conn.execute(_ADD_COLUMN)
            conn.commit()
            print(f"  {TABLE}.{COLUMN}: added.")

        # Phase 2: every link and the index, together or not at all. The index must
        # never be created over a half-linked table.
        for row_id, txn_id in links:
            conn.execute(_LINK, (txn_id, row_id))
        conn.execute(_CREATE_INDEX)
        conn.commit()

        print("  index %s: %s" % (INDEX, "already present" if has_index else "created"))
        if not links and not unmatched:
            print("  no ic_elimination row needed linking; generation is idempotent "
                  "from here.")
        print("  gl_entry was not read for a write and not written to.")
        return {"column_added": not has_column, "links": links,
                "unmatched": [r["id"] for r, _ in unmatched],
                "index_created": not has_index, "report_only": False}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 036: elimination entries record their source "
                    "intercompany transaction")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would link; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 036 "
          + ("report complete (no writes)." if args.report_only else "complete."))
