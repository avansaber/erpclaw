"""Migration 033: re-derive billing_period.status from the linked invoice (F22/N8).

Nik's decision N8 changed the meaning of `billing_period.status = 'invoiced'`:
it now means the linked `sales_invoice` is GL-posted (submitted), not merely
that a DRAFT was created. Existing installs carry periods stamped 'invoiced'
against draft invoices (the old `generate-invoices` stamped at draft creation);
this migration re-derives their true state from each linked invoice's current
status, so the books and the flag agree.

Per `billing_period` **whose own status is 'rated' or 'invoiced' (C7)** and
whose `invoice_id` is non-null:
  - linked invoice is a **draft**      ⇒ status='rated', invoiced_at=NULL
                                          (keep the link — it is the
                                          double-generation guard)
  - linked invoice is **GL-posted**    ⇒ leave 'invoiced', keep the existing
    (submitted / partially_paid /        invoiced_at (the draft→submit gap is
     paid / overdue)                      not recoverable — rewriting it would
                                          invent data)
  - linked invoice is **cancelled**    ⇒ status='rated', invoice_id=NULL,
                                          invoiced_at=NULL
  - linked invoice **row is missing**  ⇒ report, do not touch (a dangling
                                          pointer is an operator decision)

Correction C7 (Wave-G SIM finding 7, REQUIRED): the predicate is bound to
periods whose OWN status is 'rated' or 'invoiced'. Every other status ('paid',
'disputed', 'void') is REPORTED, never touched — a keyed-only-on-the-invoice
predicate resurrected a hand-set 'void' period in the probe. The
'invoiced'-with-NULL-`invoice_id` class (invisible to the non-null-invoice_id
iteration) is reported too.

Idempotent both dialects: only rows whose derived target differs from their
current state are written, so a correctly-migrated install stays byte-identical
across re-runs. Invents no data. `--report-only` prints the plan without
writing. Follows the 030 idiom (dialect branch, `run_migration(db_path)`).

AUDIT TRAIL (M102). Every period this run re-derives gets ONE `audit_log` row
carrying its status, `invoice_id` and `invoiced_at` before and after, plus the
linked invoice's status that decided it. Written on the SAME cursor inside the
SAME transaction as the UPDATE, so report mode and a crashed run leave no trail
and a committed change always has one; and because only rows whose target
differs are written at all, a re-run on a migrated install writes neither an
UPDATE nor an audit row. Read it back with

    get-audit-log --audit-action "migration:033_rederive_billing_period_invoice_state"

This one is worth stating plainly: the migration RE-DERIVES a state flag from
another table, so without the trail there is no record anywhere that the flag was
ever anything else. Convention + gate:
planning/simlogs/m102_SIM_2026-08-12.md.
"""
import argparse
import importlib.util
import os
import sys
from datetime import datetime, timezone

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib.audit import migration_action, migration_audit_statement  # noqa: E402

DEFAULT_DB_PATH = os.path.join(
    os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")),
    "data.sqlite")

# M102: derived from the filename, never typed, so the trail's action string
# cannot drift from the stem migration_runner ledgers this file under.
MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]

# Rewrites billing_period.status / invoice_id / invoiced_at on rows selected from
# the install's own data (M102 §3).
MIGRATION_DATA_CLASS = "rows"

# sales_invoice statuses that mean "GL-posted" — the biconditional's RHS. Kept
# local so the migration stays standalone (030 precedent). Must match
# erpclaw-billing/db_query.py::_GL_POSTED_INVOICE_STATUSES.
_GL_POSTED_INVOICE_STATUSES = ("submitted", "partially_paid", "paid", "overdue")
_WRITABLE_PERIOD_STATUSES = ("rated", "invoiced")


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _table_exists(cur, name, dialect):
    if dialect == "postgresql":
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s", (name,))
    else:
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,))
    return cur.fetchone() is not None


def _bind(sql, ph):
    """Bind the active paramstyle into a '?'-written statement.

    Only used for the audit statement, which erpclaw_lib hands over written with
    SQLite's placeholder. Every other statement in this file is assembled with
    `ph` already in it.
    """
    return sql if ph == "?" else sql.replace("?", ph)


def _audit(period_id, before, after, invoice_status):
    """(sql, params) for the audit row of one re-derived period (M102).

    Only the three columns this migration can write are carried, as they were
    and as they now are, plus the linked invoice status that decided it — which
    is the fact that makes the row reviewable rather than merely true.
    """
    changed = {k: v for k, v in after.items() if before.get(k) != v}
    return migration_audit_statement(
        MIGRATION_ID, "billing_period", period_id,
        old_values={k: before.get(k) for k in changed},
        new_values=changed,
        description="migration %s re-derived billing_period %s from its linked "
                    "invoice (invoice status %r): %s"
                    % (MIGRATION_ID, period_id, invoice_status,
                       ", ".join("%s %r -> %r" % (k, before.get(k), v)
                                 for k, v in sorted(changed.items()))))


def _plan(cur, ph, has_sales_invoice):
    """Compute the re-derivation plan without writing.

    Returns (updates, report) where updates is a list of
    (sql, params, audit_sql, audit_params) and report is a dict of the
    reported-not-touched classes (C7). The audit statement travels WITH its
    update so the two cannot be applied apart (M102)."""
    updates = []
    report = {"draft_to_rated": 0, "left_invoiced": 0, "cancelled_reverted": 0,
              "protected": [], "dangling": [], "invoiced_null_link": [],
              "unknown_invoice_status": []}

    # Parameterized SQL built by concatenation of literal fragments with the
    # dialect placeholder token (`?` / `%s`) — never an f-string and never a
    # value interpolation (plan §5: "no f-string interpolation").
    sel_invoice = "SELECT status FROM sales_invoice WHERE id = " + ph
    upd_cancelled = ("UPDATE billing_period SET status = 'rated', "
                     "invoice_id = NULL, invoiced_at = NULL, updated_at = "
                     + ph + " WHERE id = " + ph)
    upd_draft = ("UPDATE billing_period SET status = 'rated', "
                 "invoiced_at = NULL, updated_at = " + ph + " WHERE id = " + ph)
    upd_gl_posted = ("UPDATE billing_period SET status = 'invoiced', "
                     "updated_at = " + ph + " WHERE id = " + ph)

    cur.execute("SELECT id, status, invoice_id, invoiced_at FROM billing_period "
                "WHERE invoice_id IS NOT NULL OR status = 'invoiced'")
    rows = cur.fetchall()
    now = _now()

    for row in rows:
        pid, status, invoice_id, invoiced_at = row[0], row[1], row[2], row[3]

        if status not in _WRITABLE_PERIOD_STATUSES:
            # 'paid' / 'disputed' / 'void' — reported (it carries a link a naive
            # writer would have grabbed), never touched.
            report["protected"].append({"billing_period_id": pid, "status": status,
                                        "invoice_id": invoice_id})
            continue

        if invoice_id is None:
            # own status is 'invoiced' (the only writable status that reaches here
            # with a NULL link) — the C7 anomaly, invisible to a link iteration.
            report["invoiced_null_link"].append({"billing_period_id": pid})
            continue

        if not has_sales_invoice:
            report["dangling"].append({"billing_period_id": pid,
                                       "invoice_id": invoice_id})
            continue

        cur.execute(sel_invoice, (invoice_id,))
        inv = cur.fetchone()
        if inv is None:
            report["dangling"].append({"billing_period_id": pid,
                                       "invoice_id": invoice_id})
            continue
        inv_status = inv[0]

        before = {"status": status, "invoice_id": invoice_id,
                  "invoiced_at": invoiced_at}

        if inv_status == "cancelled":
            if status != "rated" or invoice_id is not None or invoiced_at is not None:
                updates.append((upd_cancelled, (now, pid)) + _audit(
                    pid, before, {"status": "rated", "invoice_id": None,
                                  "invoiced_at": None}, inv_status))
                report["cancelled_reverted"] += 1
        elif inv_status == "draft":
            if status != "rated" or invoiced_at is not None:
                updates.append((upd_draft, (now, pid)) + _audit(
                    pid, before, {"status": "rated", "invoice_id": invoice_id,
                                  "invoiced_at": None}, inv_status))
                report["draft_to_rated"] += 1
        elif inv_status in _GL_POSTED_INVOICE_STATUSES:
            # leave 'invoiced'; keep the existing invoiced_at (do not invent).
            if status != "invoiced":
                updates.append((upd_gl_posted, (now, pid)) + _audit(
                    pid, before, {"status": "invoiced", "invoice_id": invoice_id,
                                  "invoiced_at": invoiced_at}, inv_status))
                report["left_invoiced"] += 1
        else:
            report["unknown_invoice_status"].append(
                {"billing_period_id": pid, "invoice_id": invoice_id,
                 "invoice_status": inv_status})

    return updates, report


def _print_report(updates, report, report_only):
    verb = "would change" if report_only else "changed"
    print(f"  billing_period re-derivation ({'report-only' if report_only else 'applied'}):")
    print(f"    draft-linked → rated:        {report['draft_to_rated']}")
    print(f"    cancelled-linked → rated:    {report['cancelled_reverted']}")
    print(f"    GL-posted, ensured invoiced: {report['left_invoiced']}")
    print(f"    total rows {verb}:           {len(updates)}")
    if report["protected"]:
        print(f"    REPORTED, not touched — protected status (paid/disputed/void): "
              f"{len(report['protected'])}")
    if report["invoiced_null_link"]:
        print(f"    REPORTED, not touched — 'invoiced' with NULL link (C7): "
              f"{len(report['invoiced_null_link'])}")
    if report["dangling"]:
        print(f"    REPORTED, not touched — dangling invoice link: "
              f"{len(report['dangling'])}")
    if report["unknown_invoice_status"]:
        print(f"    REPORTED, not touched — unrecognized invoice status: "
              f"{len(report['unknown_invoice_status'])}")
    if updates and not report_only:
        print(f"    audit trail: {len(updates)} audit_log row(s), committed with "
              f"the change. Read them back with:  get-audit-log "
              f'--audit-action "{migration_action(MIGRATION_ID)}"')
    elif updates:
        print(f"    report-only: no audit_log row is written — a trail for a "
              f"change that did not happen would be the lie M102 exists to "
              f"prevent. The real run writes {len(updates)}.")


def _run(conn, ph, dialect, report_only):
    cur = conn.cursor()
    if not _table_exists(cur, "billing_period", dialect):
        print("  billing_period table absent — nothing to re-derive.")
        return
    has_si = _table_exists(cur, "sales_invoice", dialect)
    updates, report = _plan(cur, ph, has_si)
    if not report_only:
        for sql, params, audit_sql, audit_params in updates:
            cur.execute(sql, params)
            # M102 — immediately after its own UPDATE, same cursor, same
            # transaction. The pair is built together in _plan precisely so no
            # later edit can apply one without the other.
            cur.execute(_bind(audit_sql, ph), audit_params)
        conn.commit()
    _print_report(updates, report, report_only)


def _run_sqlite(path, report_only):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    try:
        _run(conn, "?", "sqlite", report_only)
    finally:
        conn.close()


def _run_postgres(url, report_only):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        _run(conn, "%s", "postgresql", report_only)
    finally:
        conn.close()


def run_migration(db_path=None, report_only=False):
    if _get_dialect() == "postgresql":
        url = os.environ.get("ERPCLAW_DB_URL") or db_path
        if not url:
            print("Postgres dialect set but no connection URL (ERPCLAW_DB_URL). "
                  "Nothing to migrate.")
            return
        _run_postgres(url, report_only)
        return
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(path):
        print(f"Database not found at {path}. Nothing to migrate.")
        return
    _run_sqlite(path, report_only)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 033: re-derive billing_period invoice state (F22/N8)")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="print the plan without writing")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("Migration 033 complete.")
