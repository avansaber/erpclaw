"""Shared audit logging for ERPClaw skill scripts.

Replaces the _audit() function that was duplicated (with only the skill
name differing) across all 24 skill db_query.py files.

Usage:
    from erpclaw_lib.audit import audit
    audit(conn, "erpclaw-selling", "add-customer", "customer", cust_id,
          new_values={"name": "Acme"}, description="Created customer")

    # When the audit write must never abort the caller's main operation,
    # but a broken audit trail should still be visible (not swallowed):
    from erpclaw_lib.audit import audit_safe
    audit_safe(conn, "erpclaw-selling", "add-customer", "customer", cust_id,
               new_values={"name": "Acme"})
"""
import json
import os
import sys
import uuid


def audit(conn, skill: str, action: str, entity_type: str, entity_id: str,
          old_values=None, new_values=None, description: str = ""):
    """Write an audit log entry.

    Args:
        conn: Active sqlite3 connection (caller manages the transaction).
        skill: Skill name, e.g. 'erpclaw-selling'.
        action: Action that triggered the audit, e.g. 'add-customer'.
        entity_type: Type of entity affected, e.g. 'customer'.
        entity_id: Primary key of the affected entity.
        old_values: Dict of previous values (optional, JSON-serialized).
        new_values: Dict of new values (optional, JSON-serialized).
        description: Human-readable description of the change.
    """
    conn.execute(
        """INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id,
           old_values, new_values, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            os.environ.get("OPENCLAW_USER"),
            skill,
            action,
            entity_type,
            entity_id,
            json.dumps(old_values) if old_values else None,
            json.dumps(new_values) if new_values else None,
            description,
        ),
    )


def audit_safe(conn, skill: str, action: str, entity_type: str, entity_id: str,
               old_values=None, new_values=None, description: str = ""):
    """Write an audit log entry that never aborts the caller's main operation.

    Same arguments as ``audit()``. The difference is failure handling:
    audit logging is best-effort, so a write failure must not roll back the
    business transaction the caller already committed. But a *silently*
    broken audit trail is its own hole — if the log stops working, someone
    needs to see it.

    Behaviour:
      - missing ``audit_log`` table (minimal installs): tolerated silently.
      - any other database error: surfaced on stderr as a WARN, not raised.
      - non-database errors (bugs): propagate normally — they are not the
        "best-effort logging" case and should not be hidden.

    Dialect-agnostic: the except classes come from
    ``erpclaw_lib.db.db_error_types()``, so this is correct on both SQLite
    and PostgreSQL. Replaces the ``try: audit(...) except Exception: pass``
    anti-pattern that swallowed real failures.
    """
    from erpclaw_lib.db import db_error_types
    missing_table, db_error = db_error_types()
    try:
        audit(conn, skill, action, entity_type, entity_id,
              old_values=old_values, new_values=new_values, description=description)
    except missing_table:
        pass  # audit_log absent on minimal installs; fall through silently
    except db_error as e:
        print(f"WARN: audit log write failed for {skill}/{action} "
              f"{entity_type}={entity_id}: {e}", file=sys.stderr)
