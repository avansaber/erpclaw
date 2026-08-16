"""ERPClaw Advanced Accounting -- Multi-Entity Consolidation domain module

Actions for consolidation groups, group entities, and elimination entries (3 tables, 8 actions).
Imported by db_query.py (unified router).
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

try:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.naming import get_next_name, ENTITY_PREFIXES
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit

    ENTITY_PREFIXES.setdefault("consolidation_group", "CGRP-")
except ImportError:
    pass

SKILL = "erpclaw-accounting-adv"

_now_iso = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------
VALID_GROUP_STATUSES = ("active", "inactive")
VALID_CONSOLIDATION_METHODS = ("full", "proportional", "equity")
VALID_ENTRY_TYPES = ("ic_elimination", "minority_interest", "currency_translation", "goodwill")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _validate_company(conn, company_id):
    if not company_id:
        err("--company-id is required")
    if not conn.execute("SELECT id FROM company WHERE id = ?", (company_id,)).fetchone():
        err(f"Company {company_id} not found")


def _validate_group(conn, group_id):
    if not group_id:
        err("--group-id is required")
    row = conn.execute("SELECT id FROM advacct_consolidation_group WHERE id = ?", (group_id,)).fetchone()
    if not row:
        err(f"Consolidation group {group_id} not found")


# ===========================================================================
# 1. add-consolidation-group
# ===========================================================================
def add_consolidation_group(conn, args):
    _validate_company(conn, args.company_id)

    name = getattr(args, "name", None)
    if not name:
        err("--name is required")

    group_id = str(uuid.uuid4())
    conn.company_id = args.company_id
    naming = get_next_name(conn, "consolidation_group", company_id=args.company_id)
    now = _now_iso()

    conn.execute("""
        INSERT INTO advacct_consolidation_group (
            id, naming_series, name, parent_company_id, consolidation_currency,
            group_status, company_id, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        group_id, naming, name,
        getattr(args, "parent_company_id", None),
        getattr(args, "consolidation_currency", None) or "USD",
        "active", args.company_id, now, now,
    ))
    audit(conn, SKILL, "add-consolidation-group", "advacct_consolidation_group", group_id,
          new_values={"name": name})
    conn.commit()
    ok({
        "id": group_id, "naming_series": naming, "name": name,
        "group_status": "active",
    })


# ===========================================================================
# 2. list-consolidation-groups
# ===========================================================================
def list_consolidation_groups(conn, args):
    where, params = ["1=1"], []
    if getattr(args, "company_id", None):
        where.append("company_id = ?")
        params.append(args.company_id)
    if getattr(args, "group_status", None):
        where.append("group_status = ?")
        params.append(args.group_status)
    if getattr(args, "search", None):
        where.append("(LOWER(name) LIKE LOWER(?))")
        params.append(f"%{args.search}%")

    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM advacct_consolidation_group WHERE {where_sql}", params
    ).fetchone()[0]
    params.extend([args.limit, args.offset])
    rows = conn.execute(
        f"SELECT * FROM advacct_consolidation_group WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params
    ).fetchall()
    ok({
        "rows": [row_to_dict(r) for r in rows],
        "total_count": total, "limit": args.limit, "offset": args.offset,
        "has_more": (args.offset + args.limit) < total,
    })


# ===========================================================================
# 3. add-group-entity
# ===========================================================================
def add_group_entity(conn, args):
    group_id = getattr(args, "group_id", None)
    _validate_group(conn, group_id)
    _validate_company(conn, args.company_id)

    entity_company_id = getattr(args, "entity_company_id", None)
    if not entity_company_id:
        err("--entity-company-id is required")

    entity_name = getattr(args, "entity_name", None)
    if not entity_name:
        err("--entity-name is required")

    consolidation_method = getattr(args, "consolidation_method", None) or "full"
    if consolidation_method not in VALID_CONSOLIDATION_METHODS:
        err(f"Invalid consolidation-method: {consolidation_method}. Must be one of: {', '.join(VALID_CONSOLIDATION_METHODS)}")

    ownership_pct = getattr(args, "ownership_pct", None) or "100"

    entity_id = str(uuid.uuid4())
    now = _now_iso()

    conn.execute("""
        INSERT INTO advacct_group_entity (
            id, group_id, entity_company_id, entity_name, ownership_pct,
            functional_currency, consolidation_method, is_active,
            company_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        entity_id, group_id, entity_company_id, entity_name, ownership_pct,
        getattr(args, "functional_currency", None) or "USD",
        consolidation_method, 1,
        args.company_id, now,
    ))
    audit(conn, SKILL, "add-group-entity", "advacct_group_entity", entity_id,
          new_values={"group_id": group_id, "entity_name": entity_name, "ownership_pct": ownership_pct})
    conn.commit()
    ok({
        "id": entity_id, "group_id": group_id,
        "entity_company_id": entity_company_id, "entity_name": entity_name,
        "ownership_pct": ownership_pct, "consolidation_method": consolidation_method,
    })


# ===========================================================================
# 4. run-consolidation
# ===========================================================================
def run_consolidation(conn, args):
    group_id = getattr(args, "group_id", None)
    _validate_group(conn, group_id)

    period_date = getattr(args, "period_date", None)
    if not period_date:
        err("--period-date is required")

    # Get group info
    group = row_to_dict(conn.execute(
        "SELECT * FROM advacct_consolidation_group WHERE id = ?", (group_id,)
    ).fetchone())

    # Get entities
    entities = conn.execute(
        "SELECT * FROM advacct_group_entity WHERE group_id = ? AND is_active = 1",
        (group_id,)
    ).fetchall()

    if not entities:
        err("Consolidation group has no active entities")

    entity_list = [row_to_dict(e) for e in entities]
    entity_count = len(entity_list)

    audit(conn, SKILL, "run-consolidation", "advacct_consolidation_group", group_id,
          new_values={"period_date": period_date, "entity_count": entity_count})
    conn.commit()
    ok({
        "group_id": group_id, "group_name": group["name"],
        "period_date": period_date, "entity_count": entity_count,
        "entities": [{"entity_name": e["entity_name"], "ownership_pct": e["ownership_pct"],
                      "consolidation_method": e["consolidation_method"]} for e in entity_list],
        "consolidation_run": "completed",
    })


# ===========================================================================
# 5. generate-elimination-entries
# ===========================================================================
#
# Re-running this is a normal thing to do (M95). A controller posts more
# intercompany activity into an open period and generates again; an agent
# following the M63-C steer arrives here having no idea whether the flow was run
# before. So generation must be a function of what is NOT yet eliminated, never
# a blind insert of everything posted.
#
# The unit of "already eliminated" is (group, period, source transaction), which
# is why the row carries source_ic_transaction_id. Coarser keys were measured and
# rejected in planning/simlogs/m95_SIM_2026-08-12.md §1: a (group, period) key
# cannot let new activity through, and any key derived from the row's CONTENT
# collapses two real transactions of the same shape (same from/to/type/amount
# produces byte-identical rows) and silently under-eliminates.
#
# Skip, not supersede: a posted IC transaction is immutable in practice
# (update-ic-transaction refuses anything past pending_approval, and there is no
# un-post), so an elimination derived from one can never go stale.

_SOURCE_LINK = "source_ic_transaction_id"
_SOURCE_INDEX = "uq_advacct_ee_source"


def _already_eliminated(conn, group_id, period_date):
    """Source transaction ids already eliminated for this group and period.

    An install running new code against a pre-M95 schema would otherwise
    duplicate silently, which is the exact defect this is fixing, so the missing
    column is turned into a directed instruction rather than a raw SQL error.

    EVERY OTHER FAILURE IS RE-RAISED, and that `raise` is load-bearing rather
    than tidy: this function's answer is the set of things NOT to do again, so a
    swallowed error returning an empty set would mean "nothing is eliminated
    yet" and re-create the duplicate this whole item exists to remove — from a
    locked database, or any other transient fault. Pinned by
    test_a_database_failure_that_is_not_the_column_is_never_swallowed.
    """
    try:
        rows = conn.execute(
            "SELECT source_ic_transaction_id FROM advacct_elimination_entry "
            "WHERE group_id = ? AND period_date = ? "
            "  AND source_ic_transaction_id IS NOT NULL",
            (group_id, period_date)
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - re-raised unless it is the column
        if _SOURCE_LINK in str(exc):
            err(f"This install's advacct_elimination_entry has no {_SOURCE_LINK} "
                "column, so elimination generation cannot tell new intercompany "
                "activity from activity it already eliminated.",
                suggestion="Run the foundation migrations first: "
                           "erpclaw-setup db_query.py --action migrate")
        raise
    return {r[0] for r in rows}


def _is_duplicate_source(exc):
    """Whether `exc` is the (group, period, source) uniqueness backstop firing.

    Read from the MESSAGE, because neither driver gives this constraint a class
    of its own and the two describe it differently: SQLite names the columns
    ("UNIQUE constraint failed: advacct_elimination_entry.group_id, ...") while
    PostgreSQL names the index ("duplicate key value violates unique constraint
    \"uq_advacct_ee_source\""). Requiring the column or the index name as well as
    the word keeps some OTHER uniqueness rule on this table from being reported
    as this one.
    """
    text = str(exc).lower()
    return "unique" in text and (_SOURCE_LINK in text or _SOURCE_INDEX in text)


def generate_elimination_entries(conn, args):
    group_id = getattr(args, "group_id", None)
    _validate_group(conn, group_id)

    period_date = getattr(args, "period_date", None)
    if not period_date:
        err("--period-date is required")

    company_id = getattr(args, "company_id", None)
    _validate_company(conn, company_id)

    # Get entities in this group
    entities = conn.execute(
        "SELECT entity_company_id FROM advacct_group_entity WHERE group_id = ? AND is_active = 1",
        (group_id,)
    ).fetchall()
    entity_company_ids = [e[0] for e in entities]

    if len(entity_company_ids) < 2:
        err("Need at least 2 active entities for elimination entries")

    # Find posted IC transactions between group entities
    placeholders = ",".join(["?"] * len(entity_company_ids))
    ic_rows = conn.execute(f"""
        SELECT * FROM advacct_ic_transaction
        WHERE ic_status = 'posted'
          AND from_company_id IN ({placeholders})
          AND to_company_id IN ({placeholders})
        ORDER BY created_at, id
    """, entity_company_ids + entity_company_ids).fetchall()

    already = _already_eliminated(conn, group_id, period_date)
    created_ids, skipped_ids = [], []
    now = _now_iso()

    for ic_row in ic_rows:
        ic = row_to_dict(ic_row)
        if ic["id"] in already:
            skipped_ids.append(ic["id"])
            continue

        # Create elimination entry (debit IC revenue, credit IC expense)
        try:
            conn.execute("""
                INSERT INTO advacct_elimination_entry (
                    id, group_id, period_date, debit_account, credit_account,
                    amount, description, entry_type, source_ic_transaction_id,
                    company_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                str(uuid.uuid4()), group_id, period_date,
                "IC Revenue", "IC Expense",
                ic["amount"],
                f"Elimination: {ic['transaction_type']} from {ic['from_company_id']} to {ic['to_company_id']}",
                "ic_elimination", ic["id"], company_id, now,
            ))
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is the backstop
            # The check above and this INSERT are not one atomic step, so a
            # second generator can commit between them. The index catches that
            # (it is there for exactly this), but a raw driver string about a
            # failed UNIQUE constraint tells neither an operator nor an agent
            # that the data is fine and a re-run finishes the job.
            if not _is_duplicate_source(exc):
                raise
            conn.rollback()
            err(f"Intercompany transaction {ic['id']} was eliminated for this "
                f"group and {period_date} by another writer between this run's "
                "check and its write, so this run wrote nothing at all.",
                suggestion="Re-run generate-elimination-entries: it eliminates "
                           "only what is still missing, so it will skip whatever "
                           "the other run created and finish the rest.")
        created_ids.append(ic["id"])

    # Three outcomes, not two. "Nothing happened" splits into "everything was
    # already eliminated" and "there was never anything to eliminate", and a
    # caller that cannot tell them apart tells a user the work is done when the
    # real answer is that they never posted their transaction.
    if created_ids:
        outcome = "created"
        message = (f"Eliminated {len(created_ids)} posted intercompany "
                   f"transaction(s) for {period_date}")
        message += (f"; {len(skipped_ids)} were already eliminated."
                    if skipped_ids else ".")
    elif skipped_ids:
        outcome = "already_eliminated"
        message = (f"No new intercompany activity for this group and period; "
                   f"{len(skipped_ids)} posted transaction(s) were already "
                   f"eliminated. Nothing was written.")
    else:
        outcome = "nothing_to_eliminate"
        message = ("No posted intercompany transactions between this group's "
                   "entities, so there is nothing to eliminate. Only "
                   "transactions in ic_status 'posted' are eliminated: "
                   "add-ic-transaction -> approve-ic-transaction -> "
                   "post-ic-transaction.")

    audit(conn, SKILL, "generate-elimination-entries", "advacct_elimination_entry", group_id,
          new_values={"period_date": period_date, "entries_created": len(created_ids),
                      "entries_skipped": len(skipped_ids), "outcome": outcome})
    conn.commit()
    ok({
        "group_id": group_id, "period_date": period_date,
        "entries_created": len(created_ids),
        "entries_skipped": len(skipped_ids),
        # Ids, not just counts: a count cannot be checked against the books.
        "eliminated_ic_transaction_ids": created_ids,
        "skipped_ic_transaction_ids": skipped_ids,
        "outcome": outcome,
        "message": message,
    })


# ===========================================================================
# 6. add-currency-translation
# ===========================================================================
def add_currency_translation(conn, args):
    group_id = getattr(args, "group_id", None)
    _validate_group(conn, group_id)
    _validate_company(conn, args.company_id)

    period_date = getattr(args, "period_date", None)
    if not period_date:
        err("--period-date is required")

    amount = getattr(args, "amount", None)
    if not amount:
        err("--amount is required")

    debit_account = getattr(args, "debit_account", None) or "CTA - Debit"
    credit_account = getattr(args, "credit_account", None) or "CTA - Credit"

    entry_id = str(uuid.uuid4())
    now = _now_iso()

    conn.execute("""
        INSERT INTO advacct_elimination_entry (
            id, group_id, period_date, debit_account, credit_account,
            amount, description, entry_type, company_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        entry_id, group_id, period_date,
        debit_account, credit_account, amount,
        getattr(args, "description", None) or "Currency translation adjustment",
        "currency_translation", args.company_id, now,
    ))
    audit(conn, SKILL, "add-currency-translation", "advacct_elimination_entry", entry_id,
          new_values={"group_id": group_id, "amount": amount, "entry_type": "currency_translation"})
    conn.commit()
    ok({
        "id": entry_id, "group_id": group_id, "period_date": period_date,
        "amount": amount, "entry_type": "currency_translation",
    })


# ===========================================================================
# 7. consolidation-trial-balance-report
# ===========================================================================
def consolidation_trial_balance_report(conn, args):
    group_id = getattr(args, "group_id", None)
    _validate_group(conn, group_id)

    period_date = getattr(args, "period_date", None)

    group = row_to_dict(conn.execute(
        "SELECT * FROM advacct_consolidation_group WHERE id = ?", (group_id,)
    ).fetchone())

    # Get entities
    entities = conn.execute(
        "SELECT * FROM advacct_group_entity WHERE group_id = ? AND is_active = 1 ORDER BY entity_name",
        (group_id,)
    ).fetchall()

    # Get elimination entries
    where_elim, params_elim = ["group_id = ?"], [group_id]
    if period_date:
        where_elim.append("period_date = ?")
        params_elim.append(period_date)

    elim_entries = conn.execute(
        f"SELECT * FROM advacct_elimination_entry WHERE {' AND '.join(where_elim)} ORDER BY created_at",
        params_elim
    ).fetchall()

    total_eliminations = sum(
        Decimal(row_to_dict(e)["amount"]) for e in elim_entries
    )

    # M114: the duplication surplus is DECIDABLE and must be visible where the
    # operator reads the number it inflates — not an unlabelled null per row.
    surplus_rows, surplus_total = _surplus_rows(conn, group_id, period_date)
    unlinked = {
        "count": len(surplus_rows),
        "total_amount": str(surplus_total),
    }
    if surplus_rows:
        unlinked["warning"] = (
            "these ic_elimination rows have no source intercompany "
            "transaction (pre-M95 duplication residue) and INFLATE "
            "total_eliminations. Review with list-elimination-surplus; "
            "correct with remove-elimination-surplus.")

    ok({
        "report": "consolidation_trial_balance",
        "group_id": group_id, "group_name": group["name"],
        "period_date": period_date,
        "entities": [row_to_dict(e) for e in entities],
        "elimination_entries": [row_to_dict(e) for e in elim_entries],
        "total_eliminations": str(total_eliminations),
        "unlinked_ic_eliminations": unlinked,
        "entity_count": len(entities),
    })


# ===========================================================================
# 8. consolidation-summary
# ===========================================================================
def consolidation_summary(conn, args):
    group_id = getattr(args, "group_id", None)
    _validate_group(conn, group_id)

    group = row_to_dict(conn.execute(
        "SELECT * FROM advacct_consolidation_group WHERE id = ?", (group_id,)
    ).fetchone())

    entity_count = conn.execute(
        "SELECT COUNT(*) FROM advacct_group_entity WHERE group_id = ? AND is_active = 1",
        (group_id,)
    ).fetchone()[0]

    elimination_count = conn.execute(
        "SELECT COUNT(*) FROM advacct_elimination_entry WHERE group_id = ?",
        (group_id,)
    ).fetchone()[0]

    by_type = conn.execute("""
        SELECT entry_type, COUNT(*) as cnt, SUM(CAST(amount AS NUMERIC)) as total
        FROM advacct_elimination_entry WHERE group_id = ?
        GROUP BY entry_type
    """, (group_id,)).fetchall()

    ok({
        "report": "consolidation_summary",
        "group_id": group_id,
        "group_name": group["name"],
        "group_status": group["group_status"],
        "consolidation_currency": group["consolidation_currency"],
        "entity_count": entity_count,
        "elimination_count": elimination_count,
        "eliminations_by_type": {r[0]: {"count": r[1], "total": str(Decimal(str(r[2])).quantize(Decimal("0.01")))} for r in by_type} if by_type else {},
    })


# ===========================================================================
# 9/10. list-elimination-surplus / remove-elimination-surplus (M114)
#
# An install that ran the pre-M95 elimination-duplication defect holds surplus
# `ic_elimination` rows that overstate the consolidated trial balance forever
# (measured: 79,150.00 reported where 29,150.00 is true). Migration 036 linked
# every row it could to its source intercompany transaction and deliberately
# left the surplus unlinked, so the residue is a DECIDABLE predicate:
#
#     entry_type = 'ic_elimination' AND source_ic_transaction_id IS NULL
#
# No product path writes a manual `ic_elimination` row and there is no
# delete-ic-transaction, so a false positive cannot arise through the product.
# `currency_translation` rows are legitimately unlinked (hand-authored, derived
# from nothing) and are NEVER matched by the predicate.
#
# The correction is an OPERATOR ACTION, not a migration — the M63-C precedent
# bars silent migration deletion of operator data; a gated, operator-invoked,
# fully audited removal is the consented opposite. These rows live in the
# consolidation layer only (init_schema's own note: group elimination never
# reaches `gl_entry`), so the immutable-GL rules are untouched.
#
# Plan home: planning/pending_items.md M114 (Nik go 2026-08-14);
# SIM: planning/simlogs/m114_SIM_2026-08-14.md.
# ===========================================================================

_SURPLUS_WHERE = ("entry_type = 'ic_elimination' "
                  "AND source_ic_transaction_id IS NULL")


def _surplus_rows(conn, group_id, period_date=None):
    where, params = [f"group_id = ?", ], [group_id]
    if period_date:
        where.append("period_date = ?")
        params.append(period_date)
    rows = conn.execute(
        f"SELECT * FROM advacct_elimination_entry "
        f"WHERE {' AND '.join(where)} AND {_SURPLUS_WHERE} "
        f"ORDER BY period_date, created_at",
        params
    ).fetchall()
    dicts = [row_to_dict(r) for r in rows]
    total = sum((Decimal(d["amount"]) for d in dicts), Decimal("0"))
    return dicts, total


def _require_source_link_column(args):
    """Refuse with a steer when migration 036 has not run on this install.

    Without the source link the predicate cannot distinguish surplus from
    legitimate rows, and guessing would remove an operator's real eliminations.
    Catalog question through the seam (ADR-0034), never a raw driver-side read.
    """
    from erpclaw_lib import seam
    cols = seam.column_names("advacct_elimination_entry",
                             getattr(args, "db_path", None))
    if "source_ic_transaction_id" not in cols:
        err(
            "this install has not run foundation migration 036, so elimination "
            "entries carry no source link and the surplus cannot be identified "
            "safely.",
            suggestion="Run the foundation update first (module_manager "
                       "update-foundation applies migration 036), then re-run "
                       "this action.",
        )


def list_elimination_surplus(conn, args):
    """Read-only: the unlinked ic_elimination rows for a group (M114 surface)."""
    group_id = getattr(args, "group_id", None)
    _validate_group(conn, group_id)
    _require_source_link_column(args)
    period_date = getattr(args, "period_date", None)

    rows, total = _surplus_rows(conn, group_id, period_date)
    ok({
        "group_id": group_id,
        "period_date": period_date,
        "surplus_count": len(rows),
        "surplus_total": str(total),
        "rows": rows,
        "note": ("these ic_elimination rows have no source intercompany "
                 "transaction — the pre-M95 duplication residue. They inflate "
                 "total_eliminations in the consolidated trial balance. "
                 "remove-elimination-surplus corrects them (report-only until "
                 "--confirm)." if rows else
                 "no surplus — every ic_elimination row is linked to its "
                 "source intercompany transaction."),
    })


def remove_elimination_surplus(conn, args):
    """Gated correction: delete the decidable surplus, audited row by row.

    Default is REPORT-ONLY (the migration-031 lesson: anything that changes an
    operator's numbers is previewable through the action a human runs). With
    --confirm, every deletion writes its own audit_log row carrying the full
    old row, in the SAME transaction as the delete — no removed row without its
    audit record, no audit record for a rollback.
    """
    group_id = getattr(args, "group_id", None)
    _validate_group(conn, group_id)
    _require_source_link_column(args)
    period_date = getattr(args, "period_date", None)

    rows, total = _surplus_rows(conn, group_id, period_date)
    if not rows:
        ok({"group_id": group_id, "removed": 0, "surplus_total": "0",
            "note": "no surplus to remove — every ic_elimination row is "
                    "linked to its source transaction."})

    if not getattr(args, "confirm", False):
        ok({
            "group_id": group_id, "period_date": period_date,
            "report_only": True, "would_remove": len(rows),
            "surplus_total": str(total), "rows": rows,
            "note": "report-only: nothing was removed. Re-run with --confirm "
                    "to delete exactly these rows; each deletion is audited "
                    "with the full removed row.",
        })

    for d in rows:
        audit(conn, "erpclaw-accounting-adv", "remove-elimination-surplus",
              "advacct_elimination_entry", d["id"],
              old_values=d,
              new_values={"removed": True, "reason": "M114 surplus — "
                          "unlinked ic_elimination (pre-M95 duplication)"})
        conn.execute(
            f"DELETE FROM advacct_elimination_entry "
            f"WHERE id = ? AND {_SURPLUS_WHERE}",
            (d["id"],))
    conn.commit()
    ok({
        "group_id": group_id, "period_date": period_date,
        "removed": len(rows), "surplus_total_removed": str(total),
        "note": "the consolidated trial balance for this group no longer "
                "carries the duplication surplus. Each removed row is in the "
                "audit log with its full contents.",
    })


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------
ACTIONS = {
    "add-consolidation-group": add_consolidation_group,
    "list-consolidation-groups": list_consolidation_groups,
    "add-group-entity": add_group_entity,
    "run-consolidation": run_consolidation,
    "generate-elimination-entries": generate_elimination_entries,
    "add-currency-translation": add_currency_translation,
    "consolidation-trial-balance-report": consolidation_trial_balance_report,
    "consolidation-summary": consolidation_summary,
    "list-elimination-surplus": list_elimination_surplus,
    "remove-elimination-surplus": remove_elimination_surplus,
}
