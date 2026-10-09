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

Business actions written through audit() also record the actor context (the
operating-system account plus the channel, principal claim, status and hop
list resolved by erpclaw_lib.actor) whenever the audit_log table already
carries the actor columns. The migration forms below deliberately do not:
migrations run during an upgrade before those columns exist, so their rows
keep the nine-column shape on every install.
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

_ACTOR_COLUMNS = ("actor_os_account", "actor_channel", "actor_principal_claim",
                  "actor_status", "actor_hop")

_INSERT_AUDIT_LOG_WITH_ACTOR = (
    "INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id, "
    " old_values, new_values, description, actor_os_account, actor_channel, "
    "actor_principal_claim, actor_status, actor_hop) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


_AUTHORIZATION_COLUMNS = ("authorization_id", "authorization_status")

_INSERT_AUDIT_LOG_WITH_AUTHORIZATION = (
    "INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id, "
    " old_values, new_values, description, authorization_id, "
    "authorization_status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

_INSERT_AUDIT_LOG_WITH_ACTOR_AND_AUTHORIZATION = (
    "INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id, "
    " old_values, new_values, description, actor_os_account, actor_channel, "
    "actor_principal_claim, actor_status, actor_hop, authorization_id, "
    "authorization_status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


_SCOPE_COLUMNS = ("scope_company_ids", "scope_status")

_SCOPE_STATUSES = ("in_scope", "out_of_scope", "no_scope", "no_principal",
                   "underived", "not_applicable")

_INSERT_AUDIT_LOG_WITH_SCOPE = (
    "INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id, "
    " old_values, new_values, description, scope_company_ids, "
    "scope_status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

_INSERT_AUDIT_LOG_WITH_ACTOR_AND_SCOPE = (
    "INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id, "
    " old_values, new_values, description, actor_os_account, actor_channel, "
    "actor_principal_claim, actor_status, actor_hop, scope_company_ids, "
    "scope_status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

_INSERT_AUDIT_LOG_WITH_AUTHORIZATION_AND_SCOPE = (
    "INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id, "
    " old_values, new_values, description, authorization_id, "
    "authorization_status, scope_company_ids, scope_status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

_INSERT_AUDIT_LOG_WITH_ACTOR_AUTHORIZATION_AND_SCOPE = (
    "INSERT INTO audit_log (id, user_id, skill, action, entity_type, entity_id, "
    " old_values, new_values, description, actor_os_account, actor_channel, "
    "actor_principal_claim, actor_status, actor_hop, authorization_id, "
    "authorization_status, scope_company_ids, scope_status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


_PROBE_OPEN = "SAVEPOINT erpclaw_actor_probe"
_PROBE_RELEASE = "RELEASE SAVEPOINT erpclaw_actor_probe"
_PROBE_UNWIND = "ROLLBACK TO SAVEPOINT erpclaw_actor_probe"


def _probe_actor_columns(conn) -> bool:
    """Whether this handle's audit_log carries the five actor columns.

    Decided on the caller's own handle with a fixed round trip: open the
    named savepoint, read the five columns as a zero-row bounded read built
    through the query builders, then release the savepoint. Any failure
    unwinds to the savepoint and releases it (each step guarded) and
    answers False, leaving the caller's unit of work exactly as found.
    """
    try:
        conn.execute(_PROBE_OPEN)
        from erpclaw_lib.query import Field, Q, Table
        table = Table("audit_log")
        # The fields name their table: a bare quoted name for a missing
        # column would come back as text instead of failing, so the read
        # must say whose columns it wants.
        namespaced = Table("audit_log", alias="audit_log")
        probe = Q.from_(table).select(
            *[Field(name, table=namespaced) for name in _ACTOR_COLUMNS]
        ).limit(0)
        conn.execute(probe.get_sql())
        conn.execute(_PROBE_RELEASE)
        return True
    except Exception:
        try:
            conn.execute(_PROBE_UNWIND)
        except Exception:
            pass
        try:
            conn.execute(_PROBE_RELEASE)
        except Exception:
            pass
        return False


def _has_actor_columns(conn) -> bool:
    """Whether the opened database already carries the actor columns.

    Answered once per handle by the probe above and remembered on the
    handle itself; a handle that refuses the note is simply probed again
    on the next write.
    """
    try:
        cached = getattr(conn, "_erpclaw_audit_actor_columns", None)
    except Exception:
        cached = None
    if isinstance(cached, bool):
        return cached
    result = _probe_actor_columns(conn)
    try:
        object.__setattr__(conn, "_erpclaw_audit_actor_columns", result)
    except Exception:
        pass
    return result


def _probe_authorization_columns(conn) -> bool:
    """Whether this handle's audit_log carries the authorization columns.

    Decided on the caller's own handle with a fixed round trip: open the
    named savepoint, read the two columns as a zero-row bounded read built
    through the query builders, then release the savepoint. Any failure
    unwinds to the savepoint and releases it (each step guarded) and
    answers False, leaving the caller's unit of work exactly as found.
    """
    try:
        conn.execute(_PROBE_OPEN)
        from erpclaw_lib.query import Field, Q, Table
        table = Table("audit_log")
        # The fields name their table: a bare quoted name for a missing
        # column would come back as text instead of failing, so the read
        # must say whose columns it wants.
        namespaced = Table("audit_log", alias="audit_log")
        probe = Q.from_(table).select(
            *[Field(name, table=namespaced) for name in _AUTHORIZATION_COLUMNS]
        ).limit(0)
        conn.execute(probe.get_sql())
        conn.execute(_PROBE_RELEASE)
        return True
    except Exception:
        try:
            conn.execute(_PROBE_UNWIND)
        except Exception:
            pass
        try:
            conn.execute(_PROBE_RELEASE)
        except Exception:
            pass
        return False


def _has_authorization_columns(conn) -> bool:
    """Whether the opened database already carries the authorization columns.

    Answered once per handle by the probe above and remembered on the
    handle itself; a handle that refuses the note is simply probed again
    on the next write.
    """
    try:
        cached = getattr(conn, "_erpclaw_audit_authorization_columns", None)
    except Exception:
        cached = None
    if isinstance(cached, bool):
        return cached
    result = _probe_authorization_columns(conn)
    try:
        object.__setattr__(conn, "_erpclaw_audit_authorization_columns",
                           result)
    except Exception:
        pass
    return result


def _probe_scope_columns(conn) -> bool:
    """Whether this handle's audit_log carries the scope columns.

    Decided on the caller's own handle with a fixed round trip: open the
    named savepoint, read the two columns as a zero-row bounded read built
    through the query builders, then release the savepoint. Any failure
    unwinds to the savepoint and releases it (each step guarded) and
    answers False, leaving the caller's unit of work exactly as found.
    """
    try:
        conn.execute(_PROBE_OPEN)
        from erpclaw_lib.query import Field, Q, Table
        table = Table("audit_log")
        # The fields name their table: a bare quoted name for a missing
        # column would come back as text instead of failing, so the read
        # must say whose columns it wants.
        namespaced = Table("audit_log", alias="audit_log")
        probe = Q.from_(table).select(
            *[Field(name, table=namespaced) for name in _SCOPE_COLUMNS]
        ).limit(0)
        conn.execute(probe.get_sql())
        conn.execute(_PROBE_RELEASE)
        return True
    except Exception:
        try:
            conn.execute(_PROBE_UNWIND)
        except Exception:
            pass
        try:
            conn.execute(_PROBE_RELEASE)
        except Exception:
            pass
        return False


def _has_scope_columns(conn) -> bool:
    """Whether the opened database already carries the scope columns.

    Answered once per handle by the probe above and remembered on the
    handle itself; a handle that refuses the note is simply probed again
    on the next write.
    """
    try:
        cached = getattr(conn, "_erpclaw_audit_scope_columns", None)
    except Exception:
        cached = None
    if isinstance(cached, bool):
        return cached
    result = _probe_scope_columns(conn)
    try:
        object.__setattr__(conn, "_erpclaw_audit_scope_columns", result)
    except Exception:
        pass
    return result


def scope_values(scope_company_ids, scope_status):
    if scope_status is not None and scope_status not in _SCOPE_STATUSES:
        raise ValueError("SCOPE_STATUS_INVALID")
    if scope_company_ids is not None and not (
            isinstance(scope_company_ids, (list, tuple, set, frozenset))
            and all(isinstance(item, str) and item and "," not in item
                    for item in scope_company_ids)):
        raise ValueError("SCOPE_COMPANY_IDS_INVALID")
    if scope_company_ids and scope_status is None:
        raise ValueError("SCOPE_STATUS_INVALID")
    if (scope_status in ("in_scope", "out_of_scope")
            and not scope_company_ids):
        raise ValueError("SCOPE_COMPANY_IDS_INVALID")
    if scope_company_ids is not None:
        scope_text = ",".join(sorted(set(scope_company_ids))) or None
    else:
        scope_text = None
    return (scope_text, scope_status)


def scope_columns_present(conn) -> bool:
    return _has_scope_columns(conn)

# Prefix that makes a migration's rows findable through the shipped read action:
#   get-system-audit-log --audit-action "migration:035_disposal_gain_loss_account_type"
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
          old_values=None, new_values=None, description: str = "",
          *, authorization_id=None, authorization_status=None,
          scope_company_ids=None, scope_status=None):
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
        scope_company_ids: Companies the call touched, as a list, tuple,
            set or frozenset of non-empty strings without commas (optional,
            stored de-duplicated, sorted and joined with ",").
        scope_status: Scope check outcome, one of 'in_scope',
            'out_of_scope', 'no_scope', 'no_principal', 'underived' or
            'not_applicable' (optional, stored alongside the ids).
    """
    from erpclaw_lib.authorization_envelope import (
        AUDIT_STATUSES as _AUDIT_STATUSES)
    if (authorization_status is not None
            and authorization_status not in _AUDIT_STATUSES):
        raise ValueError("AUTHORIZATION_STATUS_INVALID")
    explicit_scope = (scope_company_ids is not None
                      or scope_status is not None)
    if explicit_scope:
        scope_text, scope_status = scope_values(
            scope_company_ids, scope_status)
    else:
        try:
            from erpclaw_lib import company_scope as _scope_mod
            _note = _scope_mod.bound_note(conn)
        except Exception:
            _note = None
        if _note is None:
            scope_text, scope_status = None, None
        else:
            try:
                _ids = None
                if _note.company_ids is not None:
                    _ids = list(_note.company_ids)
                scope_text, scope_status = scope_values(
                    _ids, _note.status)
            except ValueError:
                scope_text, scope_status = None, None
            if not _has_scope_columns(conn):
                scope_text, scope_status = None, None
    has_actor = _has_actor_columns(conn)
    has_authorization = _has_authorization_columns(conn)
    if not has_authorization and (
            authorization_id is not None
            or authorization_status is not None):
        raise ValueError("AUTHORIZATION_AUDIT_UNAVAILABLE")
    has_scope = _has_scope_columns(conn)
    if not has_scope and (scope_company_ids is not None
                          or scope_status is not None):
        if explicit_scope:
            raise ValueError("SCOPE_AUDIT_UNAVAILABLE")
        scope_text, scope_status = None, None
    sql, params = _audit_statement(skill, action, entity_type, entity_id,
                                   old_values=old_values, new_values=new_values,
                                   description=description)
    if has_actor and has_authorization and has_scope:
        from erpclaw_lib import actor
        conn.execute(_INSERT_AUDIT_LOG_WITH_ACTOR_AUTHORIZATION_AND_SCOPE,
                     tuple(params) + actor.audit_values(actor.current())
                     + (authorization_id, authorization_status)
                     + (scope_text, scope_status))
        return
    if has_actor and has_authorization:
        from erpclaw_lib import actor
        conn.execute(_INSERT_AUDIT_LOG_WITH_ACTOR_AND_AUTHORIZATION,
                     tuple(params) + actor.audit_values(actor.current())
                     + (authorization_id, authorization_status))
        return
    if has_actor and has_scope:
        from erpclaw_lib import actor
        conn.execute(_INSERT_AUDIT_LOG_WITH_ACTOR_AND_SCOPE,
                     tuple(params) + actor.audit_values(actor.current())
                     + (scope_text, scope_status))
        return
    if has_actor:
        from erpclaw_lib import actor
        conn.execute(_INSERT_AUDIT_LOG_WITH_ACTOR,
                     tuple(params) + actor.audit_values(actor.current()))
        return
    if has_authorization and has_scope:
        conn.execute(_INSERT_AUDIT_LOG_WITH_AUTHORIZATION_AND_SCOPE,
                     tuple(params) + (authorization_id, authorization_status)
                     + (scope_text, scope_status))
        return
    if has_authorization:
        conn.execute(_INSERT_AUDIT_LOG_WITH_AUTHORIZATION,
                     tuple(params) + (authorization_id, authorization_status))
        return
    if has_scope:
        conn.execute(_INSERT_AUDIT_LOG_WITH_SCOPE,
                     tuple(params) + (scope_text, scope_status))
        return
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
               old_values=None, new_values=None, description: str = "",
               *, authorization_id=None, authorization_status=None,
               scope_company_ids=None, scope_status=None):
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
              old_values=old_values, new_values=new_values,
              description=description, authorization_id=authorization_id,
              authorization_status=authorization_status,
              scope_company_ids=scope_company_ids,
              scope_status=scope_status)
    except missing_table:
        pass  # audit_log absent on minimal installs; fall through silently
    except ValueError as e:
        if e.args != ("SCOPE_AUDIT_UNAVAILABLE",):
            raise
        try:
            audit(conn, skill, action, entity_type, entity_id,
                  old_values=old_values, new_values=new_values,
                  description=description,
                  authorization_id=authorization_id,
                  authorization_status=authorization_status)
        except missing_table:
            pass
        except db_error as e2:
            print(f"WARN: audit log write failed for {skill}/{action} "
                  f"{entity_type}={entity_id}: {e2}", file=sys.stderr)
        else:
            print(f"WARN: audit scope values dropped for {skill}/{action} "
                  f"{entity_type}={entity_id}: scope columns absent",
                  file=sys.stderr)
    except db_error as e:
        print(f"WARN: audit log write failed for {skill}/{action} "
              f"{entity_type}={entity_id}: {e}", file=sys.stderr)
