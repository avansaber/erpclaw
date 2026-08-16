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

    # A migration that changes data records what it moved, in the same
    # transaction as the change (M102):
    from erpclaw_lib.audit import audit_migration
    audit_migration(conn, "035_disposal_gain_loss_account_type", "account",
                    account_id, old_values={"account_type": "revenue"},
                    new_values={"account_type": "disposal_gain_loss"})
"""
import json
import os
import sys
import uuid

# The one INSERT in this file. Every entry point below builds its row through
# _audit_statement, so the audit_log shape cannot drift between the connection
# form and the statement form (M102).
_INSERT_AUDIT_LOG = (
    "INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id, "
    " old_values, new_values, description) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")

# Prefix that makes a migration's rows findable through the shipped read action:
#   get-audit-log --audit-action "migration:035_disposal_gain_loss_account_type"
# The stem is the migration_runner ledger id, so the audit trail and the
# migration ledger name the same thing the same way.
MIGRATION_ACTION_PREFIX = "migration:"


def _audit_statement(skill: str, action: str, entity_type: str, entity_id: str,
                     old_values=None, new_values=None, description: str = ""):
    """(sql, params) for one audit_log row, written with SQLite's '?' paramstyle.

    Kept separate from the execution so a caller holding a raw psycopg2 cursor
    (the pre-ADR-0034 migrations, which do their own '?' -> '%s' binding) writes
    the SAME row as a caller holding a seam connection.
    """
    return _INSERT_AUDIT_LOG, (
        str(uuid.uuid4()),
        os.environ.get("OPENCLAW_USER"),
        skill,
        action,
        entity_type,
        entity_id,
        json.dumps(old_values) if old_values else None,
        json.dumps(new_values) if new_values else None,
        description,
    )


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
    sql, params = _audit_statement(skill, action, entity_type, entity_id,
                                   old_values=old_values, new_values=new_values,
                                   description=description)
    conn.execute(sql, params)


def migration_action(migration_id: str) -> str:
    """The audit_log `action` value for a migration, e.g. 'migration:031_x'."""
    return MIGRATION_ACTION_PREFIX + migration_id


def migration_audit_statement(migration_id: str, entity_type: str, entity_id: str,
                              module_name: str = "erpclaw-setup",
                              old_values=None, new_values=None,
                              description: str = ""):
    """(sql, params) for one migration audit row — for raw-cursor callers.

    Migrations 031/032/033 predate ADR-0034 on this path: they hold a
    ``sqlite3`` or ``psycopg2`` cursor and translate '?' -> '%s' themselves
    (their ``_bind(sql, ph)``). ``conn.execute`` does not exist on a psycopg2
    connection and '?' is not its placeholder, so they cannot call
    :func:`audit_migration`. They run this statement through the binder they
    already have, inside the transaction they already have.

    Args:
        migration_id: the migration's ledger stem, e.g.
            '031_allocation_delink_and_release'.
        entity_type: the table whose row changed, e.g. 'account'.
        entity_id: that row's primary key.
        module_name: the module that owns the migration (its `skill` column).
        old_values / new_values: ONLY the columns that changed, as they were and
            as they now are. Not a whole-row snapshot — that would be a second
            copy of the operator's data in a table nobody prunes, and a reversal
            needs the changed columns and nothing else.
        description: one sentence a human reads.
    """
    return _audit_statement(module_name, migration_action(migration_id),
                            entity_type, entity_id, old_values=old_values,
                            new_values=new_values, description=description)


def audit_migration(conn, migration_id: str, entity_type: str, entity_id: str,
                    module_name: str = "erpclaw-setup",
                    old_values=None, new_values=None, description: str = ""):
    """Record one row a migration changed (M102).

    MUST be called on the migration's OWN connection, inside the SAME
    transaction as the change it describes. That is the whole property: there is
    then no audit row for a change that did not commit, and no committed change
    without its row. A migration that writes its trail on a second connection, or
    after its commit, has reintroduced the gap M102 exists to close.

    Deliberately NOT :func:`audit_safe`. For a business action the log is
    best-effort and a failed write must not roll back the work. For a migration
    the log IS the deliverable — a chart reclassification that cannot record what
    it moved is the defect — so a failure here fails the migration and the data
    change rolls back with it.

    Arguments are :func:`migration_audit_statement`'s; see it for the row shape.
    """
    sql, params = migration_audit_statement(
        migration_id, entity_type, entity_id, module_name=module_name,
        old_values=old_values, new_values=new_values, description=description)
    conn.execute(sql, params)


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
