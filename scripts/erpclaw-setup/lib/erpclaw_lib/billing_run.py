"""Crash-safe batch-orchestration registry for the billing cron-likes (Wave F S1.3).

Unifies generate-recurring-invoices (erpclaw-selling), process-recurring
(erpclaw-journals) and run-billing (erpclaw-billing) under one run/target
model. Each owning module keeps its per-item logic and passes it here as a
callback; this library owns the `billing_run` / `billing_run_target` rows and
the transaction discipline:

* ``start()`` materializes the run + its targets and COMMITS, so the target
  list survives a later crash.
* ``process_target()`` runs ONE transaction per target: claim the target row
  (status -> 'processing' — the row lock + concurrent-processor guard), invoke
  the callback (document insert + GL + PLE + cursor advance), then mark the
  target 'done' in the SAME transaction and commit. Any callback exception
  rolls the whole target back (zero partial writes, cursor un-advanced) and
  records a 'failed' target in a fresh small transaction — the run continues
  to the next target. A crash mid-target rolls back to 'pending'/'processing'
  and ``resume()`` re-runs it; because document + cursor + target status
  commit atomically, resume can never duplicate a document.
* Callbacks must RAISE on failure (never call ``err()``/``sys.exit``) and may
  raise :class:`SkipTarget` for benign per-item skips (already billed, no
  longer due). GL posts inside callbacks go through
  ``gl_posting.insert_gl_entries`` (never commits; idempotent per
  voucher+entry_set), so they participate in the target transaction.
* ``finalize()`` recomputes counters from the target rows (never trusts the
  incremental counts) and closes the run as completed / partially_completed /
  failed.
* ``resume()`` re-runs targets in ('pending','processing','failed') — failed
  targets rolled back cleanly and are retryable once their data error is
  fixed; runs in status 'completed' refuse to resume.

Transaction contract for callers: enter with NO uncommitted work (this module
commits and rolls back the shared connection), and do not commit inside
callbacks — the library owns the per-target boundary.
"""
import json
import uuid
from datetime import datetime, timezone

RUN_TYPES = ("recurring_invoices", "recurring_journals", "usage_billing", "combined")
TARGET_TYPES = ("recurring_invoice_template", "recurring_journal_template", "meter")
RESUMABLE_TARGET_STATUSES = ("pending", "processing", "failed")

_ERROR_SUMMARY_CAP = 50
_ERROR_MESSAGE_CAP = 2000


class SkipTarget(Exception):
    """Raised by a callback for a benign per-target skip (no writes wanted)."""


class BillingRunError(ValueError):
    """Raised for invalid billing_run operations (bad run id, bad state)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def start(conn, run_type, as_of_date, targets, company_id=None, params=None,
          created_by_user_id=None) -> str:
    """Open a run, materialize its targets, COMMIT. Returns the run id.

    Args:
        targets: iterable of ``(target_type, target_id)`` tuples.
        params: optional dict of the run's original parameters (stored as
            JSON; ``resume`` replays them verbatim).
    """
    if run_type not in RUN_TYPES:
        raise BillingRunError(f"Unknown run_type: {run_type}")
    run_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO billing_run (id, run_type, company_id, as_of_date, "
        "params_json, status, started_at, total_targets, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'running', ?, 0, ?, ?)",
        (run_id, run_type, company_id, as_of_date,
         json.dumps(params) if params is not None else None, now, now, now),
    )
    total = 0
    for target_type, target_id in targets:
        if target_type not in TARGET_TYPES:
            raise BillingRunError(f"Unknown target_type: {target_type}")
        conn.execute(
            "INSERT INTO billing_run_target (id, billing_run_id, target_type, "
            "target_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (str(uuid.uuid4()), run_id, target_type, str(target_id), now, now),
        )
        total += 1
    conn.execute(
        "UPDATE billing_run SET total_targets = ?, updated_at = ? WHERE id = ?",
        (total, now, run_id),
    )
    conn.commit()
    return run_id


def load_run(conn, run_id):
    """Fetch a billing_run row as a dict (None when absent)."""
    row = conn.execute(
        "SELECT * FROM billing_run WHERE id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_targets(conn, run_id, statuses=None):
    """Fetch the run's target rows (optionally filtered), creation order."""
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM billing_run_target WHERE billing_run_id = ? "
            f"AND status IN ({placeholders}) ORDER BY created_at, id",
            (run_id, *statuses),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM billing_run_target WHERE billing_run_id = ? "
            "ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def process_target(conn, run_id, target_row, callback):
    """Process ONE target in ONE transaction. Never raises for per-target
    failures — returns an outcome dict the caller folds into its output:

    ``{"status": "done", "result": <callback return>}``
    ``{"status": "skipped", "reason": str}``
    ``{"status": "failed", "error": str}``
    ``{"status": "already_done"}``  (idempotent no-op re-run)

    The claim UPDATE guards the status transition: a target already 'done' or
    'skipped' (or claimed by a concurrent processor that committed) is left
    untouched. Crash anywhere before the final commit rolls the target back
    losslessly — including the claim itself.
    """
    target_id = target_row["id"]
    now = _now()
    claimed = conn.execute(
        "UPDATE billing_run_target SET status = 'processing', attempt_at = ?, "
        "updated_at = ? WHERE id = ? AND status IN ('pending','processing','failed')",
        (now, now, target_id),
    )
    if claimed.rowcount == 0:
        conn.rollback()  # leave no open transaction behind the no-op
        return {"status": "already_done"}
    try:
        result = callback(conn, target_row)
        voucher_id = (result or {}).get("voucher_id")
        done_at = _now()
        conn.execute(
            "UPDATE billing_run_target SET status = 'done', result_voucher_id = ?, "
            "error_message = NULL, updated_at = ? WHERE id = ?",
            (voucher_id, done_at, target_id),
        )
        conn.execute(
            "UPDATE billing_run SET targets_processed = targets_processed + 1, "
            "targets_succeeded = targets_succeeded + 1, updated_at = ? WHERE id = ?",
            (done_at, run_id),
        )
        conn.commit()
        return {"status": "done", "result": result or {}}
    except SkipTarget as skip:
        conn.rollback()
        skip_at = _now()
        conn.execute(
            "UPDATE billing_run_target SET status = 'skipped', error_message = ?, "
            "attempt_at = ?, updated_at = ? WHERE id = ?",
            (str(skip)[:_ERROR_MESSAGE_CAP], skip_at, skip_at, target_id),
        )
        conn.execute(
            "UPDATE billing_run SET targets_processed = targets_processed + 1, "
            "updated_at = ? WHERE id = ?",
            (skip_at, run_id),
        )
        conn.commit()
        return {"status": "skipped", "reason": str(skip)}
    except Exception as exc:
        conn.rollback()  # callback writes + the claim, atomically discarded
        fail_at = _now()
        message = f"{exc}" or exc.__class__.__name__
        conn.execute(
            "UPDATE billing_run_target SET status = 'failed', error_message = ?, "
            "attempt_at = ?, updated_at = ? WHERE id = ?",
            (message[:_ERROR_MESSAGE_CAP], fail_at, fail_at, target_id),
        )
        conn.execute(
            "UPDATE billing_run SET targets_processed = targets_processed + 1, "
            "targets_failed = targets_failed + 1, updated_at = ? WHERE id = ?",
            (fail_at, run_id),
        )
        conn.commit()
        return {"status": "failed", "error": message}


def finalize(conn, run_id):
    """Close the run: recompute counters from target rows, set the terminal
    status, COMMIT. Returns the summary dict."""
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "skipped": 0}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM billing_run_target "
        "WHERE billing_run_id = ? GROUP BY status",
        (run_id,),
    ).fetchall():
        counts[row["status"]] = row["cnt"]
    total = sum(counts.values())
    unfinished = counts["pending"] + counts["processing"]
    if unfinished > 0:
        status = "partially_completed"
    elif counts["failed"] == 0:
        status = "completed"
    elif counts["done"] == 0 and counts["skipped"] == 0:
        status = "failed"
    else:
        status = "partially_completed"
    errors = [
        {"target_type": r["target_type"], "target_id": r["target_id"],
         "error": r["error_message"]}
        for r in conn.execute(
            "SELECT target_type, target_id, error_message FROM billing_run_target "
            "WHERE billing_run_id = ? AND status = 'failed' "
            "ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
    ][:_ERROR_SUMMARY_CAP]
    now = _now()
    conn.execute(
        "UPDATE billing_run SET status = ?, finished_at = ?, total_targets = ?, "
        "targets_processed = ?, targets_succeeded = ?, targets_failed = ?, "
        "error_summary_json = ?, updated_at = ? WHERE id = ?",
        (status, now, total,
         counts["done"] + counts["failed"] + counts["skipped"],
         counts["done"], counts["failed"],
         json.dumps(errors) if errors else None, now, run_id),
    )
    conn.commit()
    return {
        "billing_run_id": run_id,
        "status": status,
        "total_targets": total,
        "done": counts["done"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "unprocessed": unfinished,
    }


def resume(conn, run_id, callback, expected_run_type=None):
    """Re-run every resumable target of a non-completed run, then finalize.

    Returns ``{"run": run_dict, "outcomes": [(target_row, outcome), ...],
    "summary": finalize_summary}``. Raises :class:`BillingRunError` for a
    missing run, a run_type mismatch, or a completed run.
    """
    run = load_run(conn, run_id)
    if run is None:
        raise BillingRunError(f"Billing run not found: {run_id}")
    if expected_run_type and run["run_type"] != expected_run_type:
        raise BillingRunError(
            f"Billing run {run_id} has run_type '{run['run_type']}', "
            f"expected '{expected_run_type}'")
    if run["status"] == "completed":
        raise BillingRunError(
            f"Billing run {run_id} is completed; nothing to resume")
    now = _now()
    conn.execute(
        "UPDATE billing_run SET status = 'running', finished_at = NULL, "
        "updated_at = ? WHERE id = ?",
        (now, run_id),
    )
    conn.commit()
    outcomes = []
    for target_row in list_targets(conn, run_id, statuses=RESUMABLE_TARGET_STATUSES):
        outcome = process_target(conn, run_id, target_row, callback)
        outcomes.append((target_row, outcome))
    summary = finalize(conn, run_id)
    return {"run": run, "outcomes": outcomes, "summary": summary}
