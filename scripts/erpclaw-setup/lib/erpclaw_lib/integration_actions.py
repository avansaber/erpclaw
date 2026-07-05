"""Shared action helpers for the Stripe / Shopify integration addons.

These implement read + single-table-write logic that was byte-near-identical
between erpclaw-integrations-stripe and erpclaw-integrations-shopify: the
sync-job lifecycle (fail / cancel / get / list), GL-rule soft-delete, and
reconciliation-run fetch. Each helper is parameterized by the caller's table
(and audit skill / action name where it writes), so the OWNING module stays the
writer of its own tables — exactly the pattern already used by
``query.insert_row`` / ``query.dynamic_update``. Any module may read any table.

Hoisted in M31 H6 (necessity-audit duplication clusters 100, 102, 109-112).

delete_gl_rule response-shape convergence (M31 H6): the two addons had drifted
to different delete responses (Stripe ``{"gl_rule_id", "status": "deleted"}``;
Shopify ``{"id", "is_active": 0}``). Note that ``response.ok()`` always injects
``status="ok"``, so Stripe's ``status="deleted"`` never actually reached the
caller (dead field). Both addons now return the coherent, non-clobbered
``{"gl_rule_id": <id>, "is_active": 0}`` — Stripe's explicit key + Shopify's
real surviving state field — plus the "is already deleted" already-inactive
message. Recorded in both addon CHANGELOGs.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .audit import audit
from .query import Order, P, Q, Table, dynamic_update
from .response import err, ok, row_to_dict, rows_to_list


def _now_iso() -> str:
    """UTC ISO-8601 to the second with a trailing Z (matches addon now_iso())."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Sync-job lifecycle
# ---------------------------------------------------------------------------

def fail_sync_job(conn, table, job_id, error_message, records_processed=0):
    """Mark a sync job as failed with error details (shared internal helper)."""
    sql, params = dynamic_update(table, {
        "status": "failed",
        "records_processed": records_processed,
        "error_message": str(error_message)[:2000],
        "completed_at": _now_iso(),
    }, {"id": job_id})
    conn.execute(sql, params)
    conn.commit()


def cancel_sync_job_action(conn, args, table, skill, action_name):
    """Cancel a running or pending sync job (shared action body)."""
    sync_job_id = getattr(args, "sync_job_id", None)
    if not sync_job_id:
        err("--sync-job-id is required")

    t = Table(table)
    row = conn.execute(
        Q.from_(t).select(t.id, t.status).where(t.id == P()).get_sql(),
        (sync_job_id,)
    ).fetchone()
    if not row:
        err(f"Sync job {sync_job_id} not found")

    if row["status"] in ("completed", "failed", "cancelled"):
        err(f"Cannot cancel sync job in '{row['status']}' state")

    sql, params = dynamic_update(table, {
        "status": "cancelled",
        "completed_at": _now_iso(),
    }, {"id": sync_job_id})
    conn.execute(sql, params)

    audit(conn, skill, action_name, table, sync_job_id,
          new_values={"status": "cancelled"})
    conn.commit()

    ok({"sync_job_id": sync_job_id, "status": "cancelled"})


def get_sync_job_action(conn, args, table):
    """Get details of a specific sync job (shared action body)."""
    sync_job_id = getattr(args, "sync_job_id", None)
    if not sync_job_id:
        err("--sync-job-id is required")

    t = Table(table)
    row = conn.execute(
        Q.from_(t).select("*").where(t.id == P()).get_sql(),
        (sync_job_id,)
    ).fetchone()
    if not row:
        err(f"Sync job {sync_job_id} not found")

    data = row_to_dict(row)
    # Rename 'status' to 'sync_status' to avoid collision with ok() response status
    data["sync_status"] = data.pop("status", None)
    ok(data)


def list_sync_jobs_action(conn, args, table, account_field, account_arg,
                          extra_filter=None):
    """List sync jobs for an integration account with optional filters (shared).

    ``extra_filter`` is an optional ``(column, arg_attr)`` pair for the
    module-specific second filter (Stripe: object_type; Shopify: sync_type).
    """
    account_id = getattr(args, account_arg, None)
    if not account_id:
        err(f"--{account_arg.replace('_', '-')} is required")

    t = Table(table)
    q = Q.from_(t).select("*").where(
        getattr(t, account_field) == P()
    ).orderby(t.created_at, order=Order.desc)
    params = [account_id]

    status = getattr(args, "status", None)
    if status:
        q = q.where(t.status == P())
        params.append(status)

    if extra_filter:
        col, arg_attr = extra_filter
        val = getattr(args, arg_attr, None)
        if val:
            q = q.where(getattr(t, col) == P())
            params.append(val)

    limit = getattr(args, "limit", 50) or 50
    offset = getattr(args, "offset", 0) or 0
    q = q.limit(limit).offset(offset)

    rows = conn.execute(q.get_sql(), tuple(params)).fetchall()
    ok({
        "sync_jobs": rows_to_list(rows),
        "count": len(rows),
    })


# ---------------------------------------------------------------------------
# GL-rule soft-delete
# ---------------------------------------------------------------------------

def soft_delete_gl_rule(conn, args, table, skill, action_name):
    """Soft-delete a GL rule (set is_active=0). Converged response (M31 H6).

    Returns ``{"gl_rule_id": <id>, "is_active": 0}`` for both addons (the
    envelope adds ``status="ok"``).
    """
    gl_rule_id = getattr(args, "gl_rule_id", None)
    if not gl_rule_id:
        err("--gl-rule-id is required")

    t = Table(table)
    existing = conn.execute(
        Q.from_(t).select(t.id, t.is_active).where(t.id == P()).get_sql(),
        (gl_rule_id,)
    ).fetchone()
    if not existing:
        err(f"GL rule {gl_rule_id} not found")

    if existing["is_active"] == 0:
        err(f"GL rule {gl_rule_id} is already deleted")

    sql, params = dynamic_update(table, {"is_active": 0}, {"id": gl_rule_id})
    conn.execute(sql, params)

    audit(conn, skill, action_name, table, gl_rule_id,
          new_values={"is_active": 0})
    conn.commit()

    ok({"gl_rule_id": gl_rule_id, "is_active": 0})


# ---------------------------------------------------------------------------
# Reconciliation-run fetch
# ---------------------------------------------------------------------------

def get_reconciliation_run_action(conn, args, table, id_arg):
    """Get details of a specific reconciliation run (shared action body)."""
    run_id = getattr(args, id_arg, None)
    if not run_id:
        err(f"--{id_arg.replace('_', '-')} is required")

    t = Table(table)
    row = conn.execute(
        Q.from_(t).select("*").where(t.id == P()).get_sql(),
        (run_id,)
    ).fetchone()
    if not row:
        err(f"Reconciliation run {run_id} not found")

    ok(row_to_dict(row))
