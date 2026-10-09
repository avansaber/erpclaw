"""Company scope evaluator over the authority-core tables.

Read-only answers to which companies a principal may act in, plus one
verdict for a request under an install phase. A membership row with
effect allow puts a company in the principal scope; a deny row for the
same install, principal and company takes it out again. A principal
whose disabled marker is set has an empty scope.

Nothing here writes, commits, opens a connection, or reads the
environment. The caller passes the actor context in and passes the
phase it already resolved; a passed phase is used as given.

The same module also derives the set of companies one request touches
from its action declaration and argument list (see derive_companies).
That derivation reads target rows on the caller's own connection inside
the savepoint probe below and never writes.
"""

import json
import uuid
from dataclasses import dataclass

from erpclaw_lib import action_impact
from erpclaw_lib import actor
from erpclaw_lib.query import Field, P, Q, Table, fn

IN_SCOPE = "in_scope"
OUT_OF_SCOPE = "out_of_scope"
NO_SCOPE = "no_scope"
NO_PRINCIPAL = "no_principal"
UNDERIVED = "underived"
NOT_APPLICABLE = "not_applicable"

STATUSES = (
    IN_SCOPE,
    OUT_OF_SCOPE,
    NO_SCOPE,
    NO_PRINCIPAL,
    UNDERIVED,
    NOT_APPLICABLE,
)

REFUSAL_CODE = "COMPANY_SCOPE_REFUSED"

_ATTESTED_BASIS = "attested"
_CLAIM_BASIS = "claim"

_PHASES = ("STAGED", "ACTIVE")

_PROBE_OPEN = "SAVEPOINT erpclaw_scope_probe"
_PROBE_RELEASE = "RELEASE SAVEPOINT erpclaw_scope_probe"
_PROBE_UNWIND = "ROLLBACK TO SAVEPOINT erpclaw_scope_probe"

DERIVE_UNDERIVABLE = "COMPANY_UNDERIVABLE"
DERIVE_MISMATCH = "COMPANY_MISMATCH"
DERIVE_CODES = (DERIVE_UNDERIVABLE, DERIVE_MISMATCH)


class DerivationRefused(Exception):
    """A company derivation refused to answer.

    Carries exactly one code from DERIVE_CODES as ``code`` and as its
    only exception argument, so ``str(exc) == exc.code``. It never
    carries an id, a name, a table or a count; when the refusal follows
    a failed read it is chained to the original error for debugging,
    and the code stays the contract. The two codes exist for the
    caller's logic and tests only: a caller that answers a requester
    must give underivable, mismatch and out-of-scope the same response,
    so that adding a company flag cannot reveal whether a target id
    exists.
    """

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _derive_flag(argv, name):
    flag = "--" + name
    seen = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == flag:
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                raise DerivationRefused(DERIVE_UNDERIVABLE)
            seen.append(argv[index + 1])
            index += 2
        elif token.startswith(flag + "="):
            seen.append(token[len(flag) + 1:])
            index += 1
        else:
            index += 1
    if not seen:
        return None
    if seen[-1] == "":
        return None
    return seen[-1]


def _derive_prefix_probe(argv, flags):
    for token in argv:
        if not token.startswith("--"):
            continue
        part = token.split("=", 1)[0]
        if part in flags:
            continue
        if len(part) > 2 and any(flag.startswith(part) for flag in flags):
            raise DerivationRefused(DERIVE_UNDERIVABLE)


def _derive_company_statement(table_name):
    table = Table(table_name)
    namespaced = Table(table_name, alias=table_name)
    return (
        Q.from_(table)
        .select(Field("company_id", table=namespaced))
        .where(namespaced.id == P())
        .get_sql()
    )


def _derive_company_by_name(conn, name):
    company = Table("company")
    return conn.execute(
        Q.from_(company)
        .select(company.id)
        .where(fn.Lower(company.name) == P())
        .get_sql(),
        (name.strip().lower(),),
    ).fetchall()


def _derive_request_company(conn, company_id, company_name):
    if company_id is not None:
        return (True, company_id)
    if company_name is None:
        return (False, None)
    rows = _derive_company_by_name(conn, company_name)
    if len(rows) > 1:
        raise DerivationRefused(DERIVE_UNDERIVABLE)
    if not rows:
        return (True, None)
    return (True, rows[0]["id"])


def _derive_match(conn, company_id, company_name, derived):
    present, wanted = _derive_request_company(conn, company_id, company_name)
    if present and wanted not in derived:
        raise DerivationRefused(DERIVE_MISMATCH)


def _derive_ids(value):
    if not value.startswith("["):
        return [value]
    try:
        parsed = json.loads(value)
    except ValueError:
        raise DerivationRefused(DERIVE_UNDERIVABLE)
    if (
        not isinstance(parsed, list)
        or not parsed
        or any(not isinstance(entry, str) or not entry for entry in parsed)
    ):
        raise DerivationRefused(DERIVE_UNDERIVABLE)
    return parsed


def _derive_in_span(conn, work):
    try:
        conn.execute(_PROBE_OPEN)
        try:
            result = work()
        except DerivationRefused:
            try:
                conn.execute(_PROBE_RELEASE)
            except Exception:
                pass
            raise
        conn.execute(_PROBE_RELEASE)
        return result
    except DerivationRefused:
        raise
    except Exception as exc:
        try:
            conn.execute(_PROBE_UNWIND)
        except Exception:
            pass
        try:
            conn.execute(_PROBE_RELEASE)
        except Exception:
            pass
        raise DerivationRefused(DERIVE_UNDERIVABLE) from exc


def derive_companies(conn, action, argv):
    """Derive the set of company ids one request touches.

    The declaration for ``action`` says where its company comes from;
    ``argv`` is the argument list as the domain script receives it,
    without the program name. A ``row:``/``rows:`` company is read from
    the target row on this connection; a company the request also names
    must agree with the derived set or the derivation refuses with a
    mismatch. A target id that matches no row, an undeclared action, an
    unknown source, an absent argument or a failed read all refuse as
    underivable, with no id in the text. Calls that need no read execute
    no statement; every read runs inside one savepoint-probe span, so an
    uncommitted row on this connection reads back and the surrounding
    transaction is left untouched. The function never writes, never
    commits, opens no connection, and calls neither ``check``,
    ``principal_scope`` nor ``core_present``.
    """
    if not isinstance(action, str) or not action:
        raise ValueError("action must be a non-empty string")
    if not isinstance(argv, (list, tuple)) or any(
        not isinstance(token, str) for token in argv
    ):
        raise ValueError("argv must be a list or tuple of strings")
    decl = action_impact.declaration(action)
    if decl is None:
        raise DerivationRefused(DERIVE_UNDERIVABLE)
    if action_impact.validate({action: decl}):
        raise DerivationRefused(DERIVE_UNDERIVABLE)
    source = decl["company_source"]
    if source == "none":
        return frozenset()
    if source == "unknown":
        raise DerivationRefused(DERIVE_UNDERIVABLE)
    if source.startswith("arg:"):
        name = source[len("arg:"):]
        if name == "company-id":
            _derive_prefix_probe(argv, ("--company-id", "--company"))
            company_id = _derive_flag(argv, "company-id")
            if company_id is not None:
                return frozenset({company_id})
            company_name = _derive_flag(argv, "company")
            if company_name is None:
                raise DerivationRefused(DERIVE_UNDERIVABLE)

            def work():
                rows = _derive_company_by_name(conn, company_name)
                if len(rows) != 1:
                    raise DerivationRefused(DERIVE_UNDERIVABLE)
                return frozenset({rows[0]["id"]})

            return _derive_in_span(conn, work)
        _derive_prefix_probe(argv, ("--" + name,))
        value = _derive_flag(argv, name)
        if value is None:
            raise DerivationRefused(DERIVE_UNDERIVABLE)
        return frozenset({value})
    if source.startswith("rows:"):
        tables = source[len("rows:"):].split(",")
        targets = decl["target_arg"]
        _derive_prefix_probe(
            argv,
            tuple("--" + entry for entry in targets)
            + ("--company-id", "--company"),
        )
        wanted = []
        for table_name, entry in zip(tables, targets):
            value = _derive_flag(argv, entry)
            if value is None:
                raise DerivationRefused(DERIVE_UNDERIVABLE)
            wanted.append((table_name, _derive_ids(value)))
        company_id = _derive_flag(argv, "company-id")
        company_name = _derive_flag(argv, "company")

        def work():
            derived = set()
            for table_name, ids in wanted:
                statement = _derive_company_statement(table_name)
                for target_id in ids:
                    row = conn.execute(statement, (target_id,)).fetchone()
                    if row is None or row["company_id"] is None:
                        raise DerivationRefused(DERIVE_UNDERIVABLE)
                    derived.add(row["company_id"])
            _derive_match(conn, company_id, company_name, derived)
            return frozenset(derived)

        return _derive_in_span(conn, work)
    table_name = source[len("row:"):]
    target = decl["target_arg"]
    _derive_prefix_probe(argv, ("--" + target, "--company-id", "--company"))
    target_id = _derive_flag(argv, target)
    if target_id is None:
        raise DerivationRefused(DERIVE_UNDERIVABLE)
    company_id = _derive_flag(argv, "company-id")
    company_name = _derive_flag(argv, "company")

    def work():
        row = conn.execute(
            _derive_company_statement(table_name), (target_id,)
        ).fetchone()
        if row is None or row["company_id"] is None:
            raise DerivationRefused(DERIVE_UNDERIVABLE)
        derived = {row["company_id"]}
        _derive_match(conn, company_id, company_name, derived)
        return frozenset(derived)

    return _derive_in_span(conn, work)


@dataclass(frozen=True)
class Verdict:
    status: str
    refuse: bool
    code: str | None
    basis: str | None
    principal_id: str | None
    company_ids: tuple | None
    phase: str


def _probe_statement(table_name, columns):
    table = Table(table_name)
    namespaced = Table(table_name, alias=table_name)
    return Q.from_(table).select(
        *[Field(name, table=namespaced) for name in columns]
    ).limit(0)


def core_present(conn) -> bool:
    try:
        conn.execute(_PROBE_OPEN)
        conn.execute(
            _probe_statement("authority_install", ("install_id",)).get_sql()
        )
        conn.execute(
            _probe_statement(
                "authority_principal",
                ("install_id", "id", "disabled_at"),
            ).get_sql()
        )
        conn.execute(
            _probe_statement(
                "authority_membership",
                ("install_id", "principal_id", "company_id", "effect"),
            ).get_sql()
        )
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


def install_id(conn):
    if not core_present(conn):
        return None
    table = Table("authority_install")
    row = conn.execute(
        Q.from_(table).select(table.install_id).limit(1).get_sql()
    ).fetchone()
    if row is None:
        return None
    return row["install_id"]


def principal_scope(conn, install_id, principal_id):
    if type(install_id) is not str or not install_id:
        return frozenset()
    if type(principal_id) is not str or not principal_id:
        return frozenset()
    if not core_present(conn):
        return frozenset()
    principal = Table("authority_principal")
    found = conn.execute(
        Q.from_(principal)
        .select(principal.disabled_at)
        .where(principal.install_id == P())
        .where(principal.id == P())
        .get_sql(),
        (install_id, principal_id),
    ).fetchone()
    if found is None or found["disabled_at"] is not None:
        return frozenset()
    membership = Table("authority_membership")
    rows = conn.execute(
        Q.from_(membership)
        .select(membership.company_id, membership.effect)
        .where(membership.install_id == P())
        .where(membership.principal_id == P())
        .get_sql(),
        (install_id, principal_id),
    ).fetchall()
    allowed = set()
    denied = set()
    for row in rows:
        if row["effect"] == "allow":
            allowed.add(row["company_id"])
        elif row["effect"] == "deny":
            denied.add(row["company_id"])
    return frozenset(allowed - denied)


def check(conn, ctx, company_ids, phase):
    if phase is not None and phase not in _PHASES:
        raise ValueError("unknown phase")
    if ctx is not None and not isinstance(ctx, actor.ActorContext):
        raise ValueError("unknown actor context")
    if company_ids is None:
        wanted = None
    else:
        if isinstance(company_ids, (str, bytes)):
            raise ValueError("company ids must be an iterable of strings")
        try:
            wanted = list(company_ids)
        except Exception:
            raise ValueError("company ids must be an iterable of strings")
        for entry in wanted:
            if type(entry) is not str or not entry:
                raise ValueError("company ids must be non-empty strings")
    present = core_present(conn)
    if phase is None:
        if present:
            raise ValueError("phase is required when the core is present")
        decided = "STAGED"
    else:
        decided = phase
    principal = None
    basis = None
    if ctx is not None and (
        ctx.status == actor.ATTESTED or ctx.status == actor.CLAIMED
    ):
        claim = ctx.principal_claim
        if type(claim) is str and claim:
            principal = claim
            if ctx.status == actor.ATTESTED:
                basis = _ATTESTED_BASIS
            else:
                basis = _CLAIM_BASIS
    if not present or principal is None:
        scope = frozenset()
    else:
        scope = principal_scope(conn, install_id(conn), principal)
    if principal is None:
        status = NO_PRINCIPAL
    elif not scope:
        status = NO_SCOPE
    elif wanted is None:
        status = NOT_APPLICABLE
    elif not wanted:
        status = UNDERIVED
    elif all(entry in scope for entry in wanted):
        status = IN_SCOPE
    else:
        status = OUT_OF_SCOPE
    if decided == "STAGED":
        refuse = False
    else:
        refuse = not (
            (status == IN_SCOPE or status == NOT_APPLICABLE)
            and basis == _ATTESTED_BASIS
        )
    if wanted is None:
        reported = None
    else:
        reported = tuple(sorted(set(wanted)))
    return Verdict(
        status=status,
        refuse=refuse,
        code=REFUSAL_CODE if refuse else None,
        basis=basis,
        principal_id=principal,
        company_ids=reported,
        phase=decided,
    )


SCOPE_AMBIGUOUS = "COMPANY_SCOPE_AMBIGUOUS"


class ScopeRefused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def install_phase(conn):
    from erpclaw_lib.db import db_error_types, get_dialect
    try:
        conn.execute(_PROBE_OPEN)
        conn.execute(
            _probe_statement(
                "authority_install", ("install_id", "phase")
            ).get_sql()
        )
        conn.execute(_PROBE_RELEASE)
    except Exception as exc:
        try:
            conn.execute(_PROBE_UNWIND)
        except Exception:
            pass
        try:
            conn.execute(_PROBE_RELEASE)
        except Exception:
            pass
        missing = db_error_types()[0]
        if isinstance(exc, missing) and (
                get_dialect() == "postgresql"
                or "no such table" in str(exc)):
            return None
        raise
    table = Table("authority_install")
    rows = conn.execute(
        Q.from_(table).select(table.phase).limit(2).get_sql()
    ).fetchall()
    if len(rows) == 0:
        return None
    if len(rows) == 1 and rows[0]["phase"] in _PHASES:
        return rows[0]["phase"]
    raise ValueError("install phase unreadable")


def resolution_scope(conn, ctx):
    try:
        phase = install_phase(conn)
    except Exception as exc:
        raise ScopeRefused(REFUSAL_CODE) from exc
    if phase is None or phase == "STAGED":
        return None
    try:
        verdict = check(conn, ctx, None, "ACTIVE")
        if verdict.refuse:
            raise ScopeRefused(REFUSAL_CODE)
        scope = principal_scope(conn, install_id(conn), verdict.principal_id)
        if not scope:
            raise ScopeRefused(REFUSAL_CODE)
    except ScopeRefused:
        raise
    except Exception as exc:
        raise ScopeRefused(REFUSAL_CODE) from exc
    return frozenset(scope)


@dataclass(frozen=True)
class Note:
    company_ids: tuple | None
    status: str

    def __post_init__(self):
        ids = self.company_ids
        if ids is not None:
            if not isinstance(ids, tuple):
                raise ValueError("company ids must be a tuple or None")
            if len(ids) == 0:
                object.__setattr__(self, "company_ids", None)
                ids = None
            else:
                for entry in ids:
                    if type(entry) is not str or not entry:
                        raise ValueError("company ids must be non-empty strings")
                if tuple(sorted(ids)) != ids or len(set(ids)) != len(ids):
                    raise ValueError("company ids must be sorted unique strings")
        if self.status not in STATUSES:
            raise ValueError("unknown scope status")
        current = self.company_ids
        if self.status in (NOT_APPLICABLE, UNDERIVED):
            if current is not None:
                raise ValueError("company ids must be None for this status")
        elif self.status in (IN_SCOPE, OUT_OF_SCOPE):
            if current is None:
                raise ValueError("company ids are required for this status")


NO_COMPANY_RESOLVES = frozenset({
    "list-payments",
    "get-unallocated-payments",
    "list-open-advances",
    "list-journal-entries",
    "list-recurring-templates",
})

INSTALL_GLOBAL_EXEMPT = frozenset({"setup-company"})

# These global diagnostic reads are deliberately not MCP/read-only pinned.
# In an active install they require an attested principal whose membership
# covers every company, which is the authority-core definition of an
# installation administrator for a cross-company read.
INSTALL_GLOBAL_READS = frozenset({"get-system-audit-log"})

_SCOPE_NOTE_ATTR = "_erpclaw_company_scope_note"


def _names_company(argv):
    for token in argv:
        if not isinstance(token, str):
            continue
        if not token.startswith("--"):
            continue
        part = token.split("=", 1)[0]
        if len(part) > 2 and "--company-id".startswith(part):
            return True
    return False


def _note_of_verdict(verdict):
    ids = verdict.company_ids
    if ids is None or len(ids) == 0:
        return Note(None, verdict.status)
    return Note(tuple(ids), verdict.status)


def gate_note(conn, action, argv, phase):
    if phase not in _PHASES:
        raise ValueError("unknown phase")
    if phase == "STAGED":
        try:
            ctx = actor.current()
            decl = action_impact.declaration(action)
            if decl is None:
                wanted = []
            else:
                source = decl.get("company_source")
                if source == "none":
                    wanted = None
                elif source == "arg:company-id" and not _names_company(
                    list(argv)
                ):
                    wanted = []
                else:
                    try:
                        wanted = derive_companies(conn, action, list(argv))
                    except DerivationRefused:
                        wanted = []
            verdict = check(conn, ctx, wanted, "STAGED")
            return _note_of_verdict(verdict)
        except Exception:
            return Note(None, UNDERIVED)
    try:
        ctx = actor.current()
    except Exception as exc:
        raise ScopeRefused(REFUSAL_CODE) from exc
    try:
        decl = action_impact.declaration(action)
    except Exception as exc:
        raise ScopeRefused(REFUSAL_CODE) from exc
    if decl is None:
        raise ScopeRefused(REFUSAL_CODE)
    source = decl.get("company_source")
    cls = decl.get("class")
    try:
        if source == "none":
            wanted = None
        elif source == "arg:company-id" and not _names_company(list(argv)):
            if action not in NO_COMPANY_RESOLVES:
                raise ScopeRefused(REFUSAL_CODE)
            try:
                scope = resolution_scope(conn, ctx)
            except ScopeRefused:
                raise
            except Exception as exc:
                raise ScopeRefused(REFUSAL_CODE) from exc
            if scope is None or len(scope) == 0:
                raise ScopeRefused(REFUSAL_CODE)
            if len(scope) > 1:
                raise ScopeRefused(SCOPE_AMBIGUOUS)
            wanted = scope
        else:
            try:
                wanted = derive_companies(conn, action, list(argv))
            except DerivationRefused as exc:
                raise ScopeRefused(REFUSAL_CODE) from exc
        verdict = check(conn, ctx, wanted, "ACTIVE")
    except ScopeRefused:
        raise
    except Exception as exc:
        raise ScopeRefused(REFUSAL_CODE) from exc
    try:
        present = core_present(conn)
    except Exception as exc:
        raise ScopeRefused(REFUSAL_CODE) from exc
    if not present:
        raise ScopeRefused(REFUSAL_CODE)
    if action in INSTALL_GLOBAL_EXEMPT:
        if (
            verdict.basis == _ATTESTED_BASIS
            and verdict.status in (NOT_APPLICABLE, NO_SCOPE)
        ):
            return _note_of_verdict(verdict)
        raise ScopeRefused(REFUSAL_CODE)
    if verdict.refuse:
        raise ScopeRefused(REFUSAL_CODE)
    if source == "none" and (
            cls != "read" or action in INSTALL_GLOBAL_READS):
        try:
            table = Table("company")
            rows = conn.execute(
                Q.from_(table).select(table.id).get_sql()
            ).fetchall()
            scope = principal_scope(
                conn, install_id(conn), verdict.principal_id
            )
            for row in rows:
                if row["id"] not in scope:
                    raise ScopeRefused(REFUSAL_CODE)
        except ScopeRefused:
            raise
        except Exception as exc:
            raise ScopeRefused(REFUSAL_CODE) from exc
    return _note_of_verdict(verdict)


def bind_note(conn, note):
    if not isinstance(note, Note):
        raise ValueError("note must be a Note")
    try:
        existing = getattr(conn, _SCOPE_NOTE_ATTR, None)
    except Exception:
        existing = None
    if existing is not None:
        raise ValueError("a note is already bound")
    token = uuid.uuid4().hex
    object.__setattr__(conn, _SCOPE_NOTE_ATTR, (note, token))
    return token


def unbind_note(conn, token):
    try:
        existing = getattr(conn, _SCOPE_NOTE_ATTR, None)
    except Exception:
        return
    if existing is None:
        return
    bound_token = None
    if isinstance(existing, tuple) and len(existing) == 2:
        bound_token = existing[1]
    if bound_token != token:
        return
    try:
        object.__delattr__(conn, _SCOPE_NOTE_ATTR)
    except AttributeError:
        pass


def _clear_note(conn):
    """only the authorization gate's STAGED bind-failure fallback calls this"""
    try:
        existing = getattr(conn, _SCOPE_NOTE_ATTR, None)
    except Exception:
        return False
    if existing is None:
        return False
    try:
        object.__delattr__(conn, _SCOPE_NOTE_ATTR)
    except Exception:
        return False
    return True


def bound_note(conn):
    try:
        existing = getattr(conn, _SCOPE_NOTE_ATTR, None)
    except Exception:
        return None
    if (
        isinstance(existing, tuple)
        and len(existing) == 2
        and isinstance(existing[0], Note)
    ):
        return existing[0]
    return None
