"""ERPClaw Advanced Accounting -- Multi-Entity Consolidation domain module

Actions for consolidation groups, group entities, and elimination entries (3 tables, 8 actions).
Imported by db_query.py (unified router).
"""
import os
import sys
import uuid
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext

try:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    from erpclaw_lib.naming import get_next_name, ENTITY_PREFIXES
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.query import DecimalSum, P, Q, Table, fn
    from erpclaw_lib.query_helpers import resolve_company_id, resolve_scope_company

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
    company_id = resolve_scope_company(conn, getattr(args, "company_id", None), getattr(args, "company_name", None))
    where, params = ["1=1"], []
    where.append("company_id = ?")
    params.append(company_id)
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
# rejected: a (group, period) key
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
    try:
        valid = Decimal(amount).is_finite()
    except Exception:
        valid = False
    if not valid:
        err(f"Invalid amount: {amount}")

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

    _elim_t = Table("advacct_elimination_entry")
    _by_type_q = (
        Q.from_(_elim_t)
        .select(
            _elim_t.entry_type,
            fn.Count("*").as_("cnt"),
            fn.Coalesce(DecimalSum(_elim_t.amount), "0").as_("total"),
        )
        .where(_elim_t.group_id == P())
        .groupby(_elim_t.entry_type)
    )
    by_type = conn.execute(_by_type_q.get_sql(), (group_id,)).fetchall()

    _by_type_out = {}
    for _row in by_type:
        _data = row_to_dict(_row)
        _raw = _data.get("total")
        if _raw is None:
            _raw = "0"
        _by_type_out[_data["entry_type"]] = {
            "count": _data["cnt"],
            "total": str(Decimal(str(_raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        }

    ok({
        "report": "consolidation_summary",
        "group_id": group_id,
        "group_name": group["name"],
        "group_status": group["group_status"],
        "consolidation_currency": group["consolidation_currency"],
        "entity_count": entity_count,
        "elimination_count": elimination_count,
        "eliminations_by_type": _by_type_out,
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
# Explicit-rate translation worksheet
# ---------------------------------------------------------------------------
def _translation_decimal(value, label, rate=False, signed=False):
    pattern = r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?" if rate else (
        r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,2})?" if signed else
        r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,2})?")
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        err(f"Invalid {label}: use a canonical decimal string")
    result = Decimal(value)
    if rate and result <= 0:
        err(f"{label} must be greater than zero")
    return result


def consolidation_translation_report(conn, args):
    """Translate one entity's base-currency trial balance without posting.

    The operator supplies rate policy and carried reporting equity. No accounting
    eligibility, currency remeasurement or retained-earnings history is inferred.
    """
    company_id = getattr(args, "company_id", None)
    entity_id = getattr(args, "entity_company_id", None)
    from erpclaw_lib import actor, company_scope
    try:
        allowed = company_scope.resolution_scope(conn, actor.current())
    except company_scope.ScopeRefused:
        err(company_scope.REFUSAL_CODE)
    if allowed is not None and (company_id not in allowed or entity_id not in allowed):
        err(company_scope.REFUSAL_CODE)
    group_t, entity_t, company_t = (Table(name) for name in (
        "advacct_consolidation_group", "advacct_group_entity", "company"))
    group_row = conn.execute(Q.from_(group_t).select("*").where(
        (group_t.id == P()) & (group_t.company_id == P())).get_sql(),
        (getattr(args, "group_id", None), company_id)).fetchone()
    entity_rows = conn.execute(Q.from_(entity_t).select("*").where(
        (entity_t.group_id == P()) & (entity_t.entity_company_id == P()) &
        (entity_t.company_id == P()) & (entity_t.is_active == 1)).get_sql(),
        (getattr(args, "group_id", None), entity_id, company_id)).fetchall()
    functional = conn.execute(Q.from_(company_t).select(company_t.default_currency).where(
        company_t.id == P()).get_sql(), (entity_id,)).fetchone()
    if not group_row or group_row["group_status"] != "active" or len(entity_rows) != 1 or not functional:
        err("An active owned consolidation group and exactly one active company entity are required")
    entity = entity_rows[0]
    if entity["consolidation_method"] != "full" or entity["functional_currency"] != functional[0]:
        err("This report requires full consolidation and functional currency matching the entity's base currency")
    if group_row["consolidation_currency"] == functional[0]:
        err("The reporting currency must differ from the entity's functional currency")
    dates = []
    for field in ("start_date", "period_date"):
        text = getattr(args, field, None)
        try:
            parsed = date.fromisoformat(text)
        except (TypeError, ValueError):
            err(f"{field.replace('_', '-')} must be YYYY-MM-DD")
        if parsed.isoformat() != text:
            err(f"{field.replace('_', '-')} must be YYYY-MM-DD")
        dates.append(text)
    start, end = dates
    if start > end:
        err("start-date cannot follow period-date")
    reference = getattr(args, "review_reference", None)
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
        err("review-reference is required and must be at most 200 characters")
    closing = _translation_decimal(getattr(args, "closing_rate", None), "closing-rate", rate=True)
    average = _translation_decimal(getattr(args, "average_rate", None), "average-rate", rate=True)
    raw = getattr(args, "translation_policy", None)
    if not isinstance(raw, str) or len(raw) > 1000000:
        err("translation-policy must be a JSON array")
    try:
        policy = json.loads(raw)
    except ValueError:
        err("translation-policy must be a JSON array")
    if not isinstance(policy, list) or not policy or len(policy) > 5000:
        err("translation-policy must contain between one and 5000 account policies")
    accounts_t, gl = Table("account"), Table("gl_entry")
    accounts = {row["id"]: row_to_dict(row) for row in conn.execute(
        Q.from_(accounts_t).select("*").where(accounts_t.company_id == P()).get_sql(), (entity_id,)).fetchall()}
    ledger = conn.execute(Q.from_(gl).join(accounts_t).on(gl.account_id == accounts_t.id).select(
        gl.account_id, gl.posting_date, gl.debit_base, gl.credit_base).where(
        (accounts_t.company_id == P()) & (gl.posting_date <= P()) & (gl.is_cancelled == 0)).get_sql(),
        (entity_id, end)).fetchall()
    if not ledger:
        err("The entity has no active ledger rows through period-date")
    with localcontext() as context:
        context.prec = 60
        balances, opening_income = {}, {}
        for row in ledger:
            debit = _translation_decimal(row["debit_base"], "ledger debit_base")
            credit = _translation_decimal(row["credit_base"], "ledger credit_base")
            account_id = row["account_id"]
            balances[account_id] = balances.get(account_id, Decimal("0")) + debit - credit
            if row["posting_date"] < start and accounts[account_id]["root_type"] in ("income", "expense"):
                opening_income[account_id] = opening_income.get(account_id, Decimal("0")) + debit - credit
        if any(opening_income.values()):
            err("Income and expense opening balances must be closed before start-date")
        if sum(balances.values(), Decimal("0")) != 0:
            err("The entity's active base-currency trial balance is not balanced")
        seen, rows = set(), []
        translated_net, exact_net = Decimal("0"), Decimal("0")
        for item in policy:
            if not isinstance(item, dict):
                err("Every translation policy must be an object")
            account_id, basis = item.get("account_id"), item.get("basis")
            if not isinstance(account_id, str) or account_id in seen or account_id not in accounts:
                err("Each policy must name a distinct account belonging to the entity")
            seen.add(account_id)
            account = accounts[account_id]
            if account["is_group"] or account["currency"] != functional[0]:
                err("Each policy account must be a leaf in the entity's functional currency")
            root, source = account["root_type"], balances.get(account_id, Decimal("0"))
            fields = {"account_id", "basis"}
            rate = None
            if root in ("asset", "liability") and basis == "closing":
                rate = closing
            elif root in ("income", "expense") and basis == "average":
                rate = average
            elif root == "equity" and basis == "historical" and item.get("equity_class") == "capital":
                fields |= {"rate", "equity_class"}
                rate = _translation_decimal(item.get("rate"), "historical rate", rate=True)
            elif root == "equity" and basis == "carry" and item.get("equity_class") in ("retained-earnings", "other-equity"):
                fields |= {"reporting_balance", "equity_class"}
            else:
                err("Policy basis must match the account root; retained earnings and other equity require carried reporting balances")
            if set(item) != fields:
                err("Translation policy fields do not match its basis")
            if source == 0 and basis != "carry":
                err("Zero-balance policies are allowed only for carried equity")
            exact = source * rate if rate is not None else _translation_decimal(
                item.get("reporting_balance"), "carried reporting_balance", signed=True)
            translated = exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            exact_net += exact
            translated_net += translated
            rows.append({"account_id": account_id, "account_name": account["name"], "root_type": root,
                         "basis": basis, "source_balance": f"{source:.2f}", "translated_balance": f"{translated:.2f}",
                         "rate": str(rate) if rate is not None else None, "equity_class": item.get("equity_class")})
        if {key for key, value in balances.items() if value != 0} - seen:
            err("translation-policy must cover every nonzero account in the active trial balance")
        debits = sum((Decimal(row["translated_balance"]) for row in rows if Decimal(row["translated_balance"]) > 0), Decimal("0"))
        credits = sum((-Decimal(row["translated_balance"]) for row in rows if Decimal(row["translated_balance"]) < 0), Decimal("0"))
        cta = -translated_net if translated_net else Decimal("0")
        ok({"report": "consolidation_translation", "group_id": group_row["id"], "company_id": company_id,
            "entity_company_id": entity_id, "start_date": start, "period_date": end,
            "functional_currency": functional[0], "reporting_currency": group_row["consolidation_currency"],
            "rate_convention": "reporting currency units per functional currency unit", "closing_rate": str(closing),
            "average_rate": str(average), "review_reference": reference.strip(), "rows": rows,
            "translated_debits_before_cta": f"{debits:.2f}", "translated_credits_before_cta": f"{credits:.2f}",
            "balancing_cta_debit": f"{max(cta, Decimal('0')):.2f}",
            "balancing_cta_credit": f"{max(-cta, Decimal('0')):.2f}",
            "balancing_cta_balance": f"{cta:.2f}", "rounding_difference": str(translated_net - exact_net),
            "balanced_debits": f"{debits + max(cta, Decimal('0')):.2f}",
            "balanced_credits": f"{credits + max(-cta, Decimal('0')):.2f}",
            "posted": False, "stored": False, "result_kind": "translation_preview",
            "scope": "One full-consolidation entity, operator-approved rates and carried equity. No remeasurement, ownership allocation, tax, eliminations, CTA rollforward, compliance certification or ledger posting."})


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------
ACTIONS = {
    "consolidation-translation-report": consolidation_translation_report,
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
