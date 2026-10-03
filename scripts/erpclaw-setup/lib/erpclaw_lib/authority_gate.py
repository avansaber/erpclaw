"""Shared phase reader, declaration registry and envelope verification."""

import contextlib
import decimal
import io
import json
import logging
import re
import uuid
from decimal import Decimal

from erpclaw_lib import action_impact
from erpclaw_lib import actor
from erpclaw_lib import authority_clock
from erpclaw_lib import authority_projections
from erpclaw_lib import authority_readiness
from erpclaw_lib import company_scope
from erpclaw_lib.audit import audit as _audit
from erpclaw_lib.audit import scope_columns_present
from erpclaw_lib.authorization_consumption import (
    CONSUMED,
    INPUT_INVALID,
    REFUSED,
    _ACTION_RE,
    _ID_RE,
    _INT_MAX,
    _check_amount_value,
    _checked_wrapper,
    canonical_binding_digest,
    consume,
)
from erpclaw_lib.authorization_envelope import (
    ENVELOPE_VERSION,
    args_digest,
    canonical_pairs,
    envelope_digest,
)
from erpclaw_lib.db import (
    ConnectionWrapper,
    PgConnectionWrapper,
    db_error_types,
)
from erpclaw_lib.query import Field, P, Q, Table

AUTHORIZATION_REFUSED = "AUTHORIZATION_REFUSED"
AUTHORITY_NOT_READY = "AUTHORITY_NOT_READY"
IMPACT_UNDECLARED = "IMPACT_UNDECLARED"
HANDLER_COMMIT_INVALID = "HANDLER_COMMIT_INVALID"
AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"

ENVELOPE_ACTIONS = dict(authority_projections.product_declarations())

SUGGESTIONS = {AUTHORIZATION_REQUIRED: "This action needs a single-use authorization issued for this exact call through issue-authorization; pass its id as --authorization-id."}
SUGGESTIONS[company_scope.SCOPE_AMBIGUOUS] = "Pass --company-id or --company naming one company you may act in."

_SQLITE_BEGIN = "BEGIN IMMEDIATE"
_SQLITE_BEGIN_READ = "BEGIN"

_LOG = logging.getLogger("erpclaw.authority")

_KEY_COLS = ("install_id", "delegation_id", "action", "currency",
             "window_start", "window_end")

_AUTH_COLS = ("id", "install_id", "principal_id", "action",
              "binding_digest", "delegation_id", "issued_at",
              "expires_at", "revoked_at", "consumed_at",
              "consumed_txn")
_SIDE_COLS = ("authorization_id", "install_id", "principal_id",
              "envelope_version", "args_digest", "issuer_id",
              "issued_route", "reason_code", "reason_text",
              "idempotency_key", "call_id", "envelope_digest")
_RESULT_COLS = ("authorization_id", "consumed_txn", "result_kind",
                "result_id", "result_status", "recorded_at")

_BLOCKED_PREFIXES = ("COMMIT", "END", "BEGIN", "ROLLBACK", "RELEASE")
_PROBE_RE = re.compile(
    r"(SAVEPOINT|RELEASE SAVEPOINT|ROLLBACK TO SAVEPOINT)"
    r"\s+ERPCLAW_[A-Z0-9_]*PROBE")

_INSTALL_PROBE_OPEN = "SAVEPOINT ERPCLAW_INSTALL_PROBE"
_INSTALL_PROBE_RELEASE = "RELEASE SAVEPOINT ERPCLAW_INSTALL_PROBE"
_INSTALL_PROBE_UNWIND = "ROLLBACK TO SAVEPOINT ERPCLAW_INSTALL_PROBE"


class AuthorityRefusal(Exception):
    """An authorization operation refused under one module code."""


def open_transaction(conn):
    """Open the caller's unit of work, refusing when one is active."""
    if type(conn) is PgConnectionWrapper:
        try:
            raw = object.__getattribute__(conn, "_conn")
            auto = raw.autocommit
            status = raw.get_transaction_status()
            from psycopg2.extensions import (
                TRANSACTION_STATUS_IDLE as _idle,
            )
        except Exception:
            raise ValueError(INPUT_INVALID)
        if auto or status != _idle:
            raise ValueError(INPUT_INVALID)
        return
    if type(conn) is not ConnectionWrapper:
        raise ValueError(INPUT_INVALID)
    try:
        raw = object.__getattribute__(conn, "_conn")
        active = raw.in_transaction
        isolation = raw.isolation_level
    except Exception:
        raise ValueError(INPUT_INVALID)
    if active or isolation is None:
        raise ValueError(INPUT_INVALID)
    conn.execute(_SQLITE_BEGIN)


def open_read_transaction(conn):
    """Open a read-only unit of work, refusing when one is active.

    The same checks as ``open_transaction``, but SQLite gets a deferred
    ``BEGIN``: its first read fixes the snapshot and no write lock is taken,
    so a read also runs on read-only storage. The caller only reads and ends
    with a rollback.
    """
    if type(conn) is not ConnectionWrapper:
        open_transaction(conn)
        return
    try:
        raw = object.__getattribute__(conn, "_conn")
        active = raw.in_transaction
        isolation = raw.isolation_level
    except Exception:
        raise ValueError(INPUT_INVALID)
    if active or isolation is None:
        raise ValueError(INPUT_INVALID)
    conn.execute(_SQLITE_BEGIN_READ)


def install_phase(conn):
    """Read the stored phase and install id on this handle only."""
    missing, base = db_error_types()
    table = Table("authority_install")
    query = Q.from_(table).select(
        Field("phase"), Field("install_id")).get_sql()
    try:
        conn.execute(_INSTALL_PROBE_OPEN)
    except Exception:
        pass
    try:
        rows = conn.execute(query).fetchall()
    except Exception as exc:
        try:
            conn.execute(_INSTALL_PROBE_UNWIND)
        except Exception:
            pass
        try:
            conn.execute(_INSTALL_PROBE_RELEASE)
        except Exception:
            pass
        if type(conn) is PgConnectionWrapper:
            if isinstance(exc, missing):
                return ("STAGED", None)
            raise AuthorityRefusal(AUTHORITY_NOT_READY)
        import sqlite3 as _sqlite3
        if (isinstance(exc, _sqlite3.OperationalError)
                and "no such table" in str(exc)):
            return ("STAGED", None)
        raise AuthorityRefusal(AUTHORITY_NOT_READY)
    try:
        conn.execute(_INSTALL_PROBE_RELEASE)
    except Exception:
        pass
    if len(rows) == 0:
        return ("STAGED", None)
    if len(rows) != 1:
        raise AuthorityRefusal(AUTHORITY_NOT_READY)
    try:
        phase = rows[0]["phase"]
        install_id = rows[0]["install_id"]
    except Exception:
        raise AuthorityRefusal(AUTHORITY_NOT_READY)
    if phase not in ("STAGED", "ACTIVE"):
        raise AuthorityRefusal(AUTHORITY_NOT_READY)
    if type(install_id) is not str or not install_id:
        raise AuthorityRefusal(AUTHORITY_NOT_READY)
    return (phase, install_id)


def _rows_where(conn, table_name, columns, filters):
    table = Table(table_name)
    query = Q.from_(table).select(
        *[Field(column) for column in columns])
    params = []
    for column, value in filters:
        if value is None:
            query = query.where(Field(column).isnull())
        else:
            query = query.where(Field(column) == P())
            params.append(value)
    return [dict(record)
            for record in conn.execute(query.get_sql(),
                                       params).fetchall()]


def _single_row(conn, table_name, columns, filters):
    rows = _rows_where(conn, table_name, columns, filters)
    if not rows:
        return None
    return rows[0]


def rights_hold(conn, install_id, principal_id, delegation_id,
                company_id, targets, action, now):
    """Whether principal and delegation still authorize the targets."""
    missing, _base = db_error_types()
    try:
        holder = _single_row(
            conn, "authority_principal",
            ["kind", "disabled_at"],
            [("install_id", install_id), ("id", principal_id)])
        if holder is None or holder["disabled_at"] is not None:
            return False
        memberships = _rows_where(
            conn, "authority_membership", ["effect"],
            [("install_id", install_id),
             ("principal_id", principal_id),
             ("company_id", company_id)])
        effects = {row["effect"] for row in memberships}
        if "allow" not in effects or "deny" in effects:
            return False
        for target in targets:
            try:
                kind = target["kind"]
                target_id = target["id"]
            except (KeyError, TypeError):
                return False
            grants = _rows_where(
                conn, "authority_right", ["effect"],
                [("install_id", install_id),
                 ("principal_id", principal_id),
                 ("company_id", company_id),
                 ("resource_kind", kind),
                 ("resource_id", target_id),
                 ("action", action)])
            grant_effects = {row["effect"] for row in grants}
            if "allow" not in grant_effects:
                return False
            if "deny" in grant_effects:
                return False
        if delegation_id is not None:
            grant = _single_row(
                conn, "authority_delegation",
                ["issuer_id", "grantee_id", "issued_at",
                 "expires_at", "revoked_at"],
                [("install_id", install_id),
                 ("id", delegation_id)])
            if grant is None or grant["revoked_at"] is not None:
                return False
            try:
                window_ok = (grant["issued_at"] <= now
                             < grant["expires_at"])
            except TypeError:
                return False
            if not window_ok:
                return False
            if grant["grantee_id"] != principal_id:
                return False
            issuer = _single_row(
                conn, "authority_principal",
                ["kind", "disabled_at"],
                [("install_id", install_id),
                 ("id", grant["issuer_id"])])
            if issuer is None or issuer["kind"] != "human":
                return False
            if issuer["disabled_at"] is not None:
                return False
            for target in targets:
                try:
                    kind = target["kind"]
                    target_id = target["id"]
                except (KeyError, TypeError):
                    return False
                scope = _single_row(
                    conn, "authority_delegation_right",
                    ["delegation_id"],
                    [("install_id", install_id),
                     ("delegation_id", delegation_id),
                     ("company_id", company_id),
                     ("resource_kind", kind),
                     ("resource_id", target_id),
                     ("action", action)])
                if scope is None:
                    return False
    except missing:
        return False
    return True


def _find_cap_row(conn, install_id, delegation_id, action,
                  currency, now):
    missing, _base = db_error_types()
    try:
        rows = _rows_where(
            conn, "authority_delegation_cap",
            ["install_id", "delegation_id", "action", "currency",
             "scale", "per_operation", "aggregate_limit",
             "window_start", "window_end"],
            [("install_id", install_id),
             ("delegation_id", delegation_id),
             ("action", action), ("currency", currency)])
    except missing:
        return None
    try:
        live = [row for row in rows
                if row["window_start"] <= now < row["window_end"]]
    except (KeyError, TypeError):
        return None
    if len(live) != 1:
        return None
    return live[0]


def _read_used(conn, cap_key):
    """Current counter text for one six-column delegation counter."""
    missing, _base = db_error_types()
    try:
        rows = _rows_where(
            conn, "authority_delegation_usage", ["used"],
            [(column, cap_key[column]) for column in _KEY_COLS])
    except missing:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    if not rows:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    return rows[0]["used"]


def record_result(conn, *, authorization_id, consumed_txn,
                  result_kind, result_id, result_status,
                  recorded_at):
    """Store one execution result in the caller's unit of work."""
    if (type(authorization_id) is not str
            or _ID_RE.fullmatch(authorization_id) is None):
        raise ValueError(INPUT_INVALID)
    if (type(consumed_txn) is not str
            or _ID_RE.fullmatch(consumed_txn) is None):
        raise ValueError(INPUT_INVALID)
    if type(result_kind) is not str or not result_kind:
        raise ValueError(INPUT_INVALID)
    if result_id is not None and type(result_id) is not str:
        raise ValueError(INPUT_INVALID)
    if type(result_status) is not str or not result_status:
        raise ValueError(INPUT_INVALID)
    if (type(recorded_at) is not int or recorded_at < 0
            or recorded_at > _INT_MAX):
        raise ValueError(INPUT_INVALID)
    _checked_wrapper(conn)
    table = Table("operation_authorization_result")
    query = Q.into(table).columns(
        "authorization_id", "consumed_txn", "result_kind",
        "result_status", "result_id", "recorded_at").insert(
        P(), P(), P(), P(), P(), P()).get_sql()
    conn.execute(query, (authorization_id, consumed_txn,
                         result_kind, result_status, result_id,
                         recorded_at))


class _DeferredHandle:
    """A handle that records commit intent instead of acting on it."""

    def __init__(self, target):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_commits", 0)
        object.__setattr__(self, "_rollbacks", 0)
        object.__setattr__(self, "_closes", 0)

    @property
    def commits(self):
        return object.__getattribute__(self, "_commits")

    @property
    def rollbacks(self):
        return object.__getattribute__(self, "_rollbacks")

    def __getattr__(self, name):
        return getattr(
            object.__getattribute__(self, "_target"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_target"), name,
                value)

    def commit(self):
        seen = object.__getattribute__(self, "_commits")
        if seen >= 1:
            raise AuthorityRefusal(HANDLER_COMMIT_INVALID)
        object.__setattr__(self, "_commits", seen + 1)

    def rollback(self):
        seen = object.__getattribute__(self, "_rollbacks")
        object.__setattr__(self, "_rollbacks", seen + 1)

    def close(self):
        seen = object.__getattribute__(self, "_closes")
        object.__setattr__(self, "_closes", seen + 1)

    def __enter__(self):
        raise AuthorityRefusal(HANDLER_COMMIT_INVALID)

    def __exit__(self, *args):
        raise AuthorityRefusal(HANDLER_COMMIT_INVALID)

    def executescript(self, *args, **kwargs):
        raise AuthorityRefusal(HANDLER_COMMIT_INVALID)

    def _guard(self, statement):
        if isinstance(statement, str):
            upper = statement.strip().upper()
            if upper.startswith(_BLOCKED_PREFIXES):
                if _PROBE_RE.fullmatch(
                        " ".join(upper.split())) is None:
                    raise AuthorityRefusal(HANDLER_COMMIT_INVALID)

    def execute(self, statement, params=None):
        self._guard(statement)
        target = object.__getattribute__(self, "_target")
        if params is None:
            return target.execute(statement)
        return target.execute(statement, params)

    def executemany(self, statement, rows):
        self._guard(statement)
        return object.__getattribute__(self, "_target").executemany(
            statement, rows)


def _envelope_state(conn, authorization_id):
    missing, _base = db_error_types()
    try:
        auth = _single_row(conn, "operation_authorization",
                           _AUTH_COLS, [("id", authorization_id)])
        side = _single_row(
            conn, "operation_authorization_envelope", _SIDE_COLS,
            [("authorization_id", authorization_id)])
        done = _single_row(
            conn, "operation_authorization_result", _RESULT_COLS,
            [("authorization_id", authorization_id)])
    except missing:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    if auth is None or side is None:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    return auth, side, done


def _checked_digests(action, argv, declaration, auth, side,
                     authorization_id, warn):
    try:
        keywords = {
            "money": declaration.get("money", ()),
            "json_args": declaration.get("json_args", ()),
            "money_json_paths": declaration.get(
                "money_json_paths"),
            "bound_args": declaration.get("bound_args", ()),
        }
        pairs = canonical_pairs(argv, **keywords)
        recomputed = args_digest(argv, **keywords)
    except ValueError:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    if recomputed != side["args_digest"]:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    try:
        sealed = {
            "v": ENVELOPE_VERSION,
            "binding_digest": auth["binding_digest"],
            "args_digest": side["args_digest"],
            "issuer_id": side["issuer_id"],
            "issued_route": side["issued_route"],
            "reason_code": side["reason_code"],
            "reason_text": side["reason_text"],
            "idempotency_key": side["idempotency_key"],
            "call_id": side["call_id"],
            "issued_at": auth["issued_at"],
        }
        expected = envelope_digest(sealed)
    except ValueError:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    if expected != side["envelope_digest"]:
        if warn:
            _LOG.warning("authorization %s: envelope digest mismatch",
                         authorization_id)
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    return pairs


def _derive_checked(declaration, conn, action, pairs):
    try:
        derived = declaration["derive"](conn, action, pairs)
        company_ids = derived["company_ids"]
        targets = derived["targets"]
        amounts = derived["amounts"]
    except AuthorityRefusal:
        raise
    except Exception:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    if (type(company_ids) is not list or len(company_ids) != 1
            or type(company_ids[0]) is not str):
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    if type(targets) is not list or type(amounts) is not list:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    return company_ids[0], targets, amounts


def _binding_principal(phase, side):
    principal = side["principal_id"]
    if phase == "ACTIVE":
        if side["issued_route"] == "staged_unattested":
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        current = actor.current()
        if (current.status != actor.ATTESTED
                or current.principal_claim != principal):
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    return principal


def _verify_cap(conn, install_id, delegation_id, action, amount,
                now):
    try:
        currency = amount["currency"]
        value = amount["value"]
        scale = amount["scale"]
    except (KeyError, TypeError):
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    cap = _find_cap_row(conn, install_id, delegation_id, action,
                        currency, now)
    if cap is None or cap.get("scale") != scale:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    try:
        over = Decimal(value) > Decimal(cap["per_operation"])
    except Exception:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    if over:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    return cap


def _apply_usage(conn, cap, amount):
    scale = cap["scale"]
    key = {column: cap[column] for column in _KEY_COLS}
    used = _read_used(conn, key)
    try:
        _check_amount_value(used, scale)
    except ValueError:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    try:
        with decimal.localcontext() as context:
            context.prec = 200
            context.traps[decimal.Inexact] = True
            new = Decimal(used) + Decimal(amount["value"])
        new_text = format(new, "f")
        _check_amount_value(new_text, scale)
        limit = Decimal(cap["aggregate_limit"])
    except Exception:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    if new > limit:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)
    table = Table("authority_delegation_usage")
    query = Q.update(table).set(Field("used"), P())
    params = [new_text]
    for column in _KEY_COLS:
        query = query.where(Field(column) == P())
        params.append(key[column])
    query = query.where(Field("used") == P())
    params.append(used)
    cursor = conn.execute(query.get_sql(), params)
    try:
        count = cursor.rowcount
    except Exception:
        try:
            cursor.close()
        except Exception:
            pass
        raise RuntimeError("AUTHORIZATION_STORAGE_ERROR")
    try:
        cursor.close()
    except Exception:
        raise RuntimeError("AUTHORIZATION_STORAGE_ERROR")
    if count != 1:
        raise AuthorityRefusal(AUTHORIZATION_REFUSED)


def verify_and_consume(conn, *, authorization_id, action, argv,
                       handler):
    """Verify one envelope, run the handler once, store its result."""
    if (type(authorization_id) is not str
            or _ID_RE.fullmatch(authorization_id) is None):
        raise ValueError(INPUT_INVALID)
    if (type(action) is not str
            or _ACTION_RE.fullmatch(action) is None):
        raise ValueError(INPUT_INVALID)
    if (type(argv) is not list
            or any(type(item) is not str for item in argv)):
        raise ValueError(INPUT_INVALID)
    if not callable(handler):
        raise ValueError(INPUT_INVALID)
    open_transaction(conn)
    buf = io.StringIO()
    token = None
    try:
        phase, phase_install = install_phase(conn)
        _checked_wrapper(conn)
        if (phase == "ACTIVE"
                and not authority_readiness.is_ready(conn)):
            raise AuthorityRefusal(AUTHORITY_NOT_READY)
        declaration = ENVELOPE_ACTIONS.get(action)
        if declaration is None:
            raise AuthorityRefusal(IMPACT_UNDECLARED)
        auth, side, _done = _envelope_state(conn,
                                            authorization_id)
        if side["install_id"] != auth["install_id"]:
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        if (phase == "ACTIVE"
                and side["install_id"] != phase_install):
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        if (auth["consumed_at"] is not None
                or auth["consumed_txn"] is not None):
            try:
                conn.rollback()
            except Exception:
                pass
            return replay(conn, authorization_id=authorization_id,
                          action=action, argv=argv)
        pairs = _checked_digests(action, argv, declaration, auth,
                                 side, authorization_id, True)
        company_id, targets, amounts = _derive_checked(
            declaration, conn, action, pairs)
        principal = _binding_principal(phase, side)
        now = authority_clock.now_ms()
        if now < auth["issued_at"]:
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        binding = {
            "version": 1,
            "install_id": auth["install_id"],
            "principal_id": principal,
            "delegation_id": auth["delegation_id"],
            "action": action,
            "company_ids": [company_id],
            "targets": targets,
            "amounts": amounts,
            "expires_at": auth["expires_at"],
        }
        delegation_id = auth["delegation_id"]
        if not rights_hold(conn, auth["install_id"],
                           principal, delegation_id,
                           company_id, targets, action, now):
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        caps = []
        if delegation_id is not None:
            for amount in amounts:
                caps.append(_verify_cap(
                    conn, auth["install_id"], delegation_id,
                    action, amount, now))
        txn_id = str(uuid.uuid4())
        try:
            outcome = consume(conn,
                              authorization_id=authorization_id,
                              binding=binding, now=now,
                              txn_id=txn_id)
        except ValueError:
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        if outcome != CONSUMED:
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        if delegation_id is not None:
            for amount, cap in zip(amounts, caps):
                _apply_usage(conn, cap, amount)
        label = ("staged_unattested"
                 if side["issued_route"] == "staged_unattested"
                 else "verified")
        from erpclaw_lib import authority_sink
        token = authority_sink.bind(conn, authority_sink.AuthorityTxn(authorization_id, action, txn_id, auth["install_id"], label))
        try:
            proxy = _DeferredHandle(conn)
            try:
                with contextlib.redirect_stdout(buf):
                    try:
                        handler(proxy)
                    except SystemExit as finished:
                        if (finished.code is not None
                                and finished.code != 0):
                            raise
            except BaseException:
                text = buf.getvalue()
                if text:
                    print(text, end="")
                raise
            if proxy.commits != 1 or proxy.rollbacks != 0:
                raise AuthorityRefusal(HANDLER_COMMIT_INVALID)
            try:
                payload = json.loads(buf.getvalue())
            except Exception:
                raise AuthorityRefusal(HANDLER_COMMIT_INVALID)
            if type(payload) is not dict or payload.get("status") != "ok":
                raise AuthorityRefusal(HANDLER_COMMIT_INVALID)
            try:
                triple = declaration["result"](payload)
                result_kind, result_id, result_status = triple
            except AuthorityRefusal:
                raise
            except Exception:
                raise AuthorityRefusal(AUTHORIZATION_REFUSED)
            if type(result_kind) is not str or not result_kind:
                raise AuthorityRefusal(AUTHORIZATION_REFUSED)
            if result_id is not None and type(result_id) is not str:
                raise AuthorityRefusal(AUTHORIZATION_REFUSED)
            if type(result_status) is not str or not result_status:
                raise AuthorityRefusal(AUTHORIZATION_REFUSED)
            record_result(conn, authorization_id=authorization_id,
                          consumed_txn=txn_id, result_kind=result_kind,
                          result_id=result_id,
                          result_status=result_status,
                          recorded_at=now)
            _audit(conn, "erpclaw-setup", action,
                   "operation_authorization", authorization_id,
                   authorization_id=authorization_id,
                   authorization_status=label)
            conn.commit()
        finally:
            authority_sink.unbind(conn, token)
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        if token is not None:
            authority_sink.clear(conn, token)
        raise
    text = buf.getvalue()
    if text:
        print(text, end="")
    return payload


def replay(conn, *, authorization_id, action, argv):
    """Return the stored result for a spent envelope without writing."""
    if (type(authorization_id) is not str
            or _ID_RE.fullmatch(authorization_id) is None):
        raise ValueError(INPUT_INVALID)
    if (type(action) is not str
            or _ACTION_RE.fullmatch(action) is None):
        raise ValueError(INPUT_INVALID)
    if (type(argv) is not list
            or any(type(item) is not str for item in argv)):
        raise ValueError(INPUT_INVALID)
    open_transaction(conn)
    try:
        phase, phase_install = install_phase(conn)
        _checked_wrapper(conn)
        if (phase == "ACTIVE"
                and not authority_readiness.is_ready(conn)):
            raise AuthorityRefusal(AUTHORITY_NOT_READY)
        declaration = ENVELOPE_ACTIONS.get(action)
        if declaration is None:
            raise AuthorityRefusal(IMPACT_UNDECLARED)
        auth, side, done = _envelope_state(conn,
                                           authorization_id)
        if side["install_id"] != auth["install_id"]:
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        if (phase == "ACTIVE"
                and side["install_id"] != phase_install):
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        if (auth["consumed_at"] is None
                or auth["consumed_txn"] is None):
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        if done is None:
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        pairs = _checked_digests(action, argv, declaration, auth,
                                 side, authorization_id, True)
        company_id, targets, _amounts = _derive_checked(
            declaration, conn, action, pairs)
        principal = _binding_principal(phase, side)
        now = authority_clock.now_ms()
        if not rights_hold(conn, auth["install_id"], principal,
                           auth["delegation_id"], company_id,
                           targets, action, now):
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        conn.rollback()
        result = {
            "status": "ok",
            "replayed": True,
            "authorization_id": authorization_id,
            "result_kind": done["result_kind"],
            "result_id": done["result_id"],
            "result_status": done["result_status"],
        }
        print(json.dumps(result))
        return result
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def split_authorization_id(argv):
    """Split one authorization id from an argv list."""
    if type(argv) is not list or any(
            type(item) is not str for item in argv):
        raise ValueError(INPUT_INVALID)
    found = []
    rest = []
    pos = 0
    while pos < len(argv):
        token = argv[pos]
        if token == "--authorization-id":
            if pos + 1 >= len(argv):
                raise ValueError(INPUT_INVALID)
            value = argv[pos + 1]
            if (type(value) is not str or not value
                    or value.startswith("-")
                    or _ID_RE.fullmatch(value) is None):
                raise ValueError(INPUT_INVALID)
            found.append(value)
            pos += 2
            continue
        if token.startswith("--authorization-id="):
            value = token[len("--authorization-id="):]
            if (not value or value.startswith("-")
                    or _ID_RE.fullmatch(value) is None):
                raise ValueError(INPUT_INVALID)
            found.append(value)
            pos += 1
            continue
        rest.append(token)
        pos += 1
    if len(found) > 1:
        raise ValueError(INPUT_INVALID)
    if len(found) == 1:
        return (rest, found[0])
    return (rest, None)


def _scope_step(conn, action, argv, phase):
    """Compute the company-scope note and end its read."""
    if phase == "ACTIVE":
        try:
            try:
                ready = authority_readiness.is_ready(conn)
            except Exception as exc:
                raise AuthorityRefusal(
                    company_scope.REFUSAL_CODE) from exc
            if not ready:
                raise AuthorityRefusal(AUTHORITY_NOT_READY)
            try:
                present = scope_columns_present(conn)
            except Exception as exc:
                raise AuthorityRefusal(
                    company_scope.REFUSAL_CODE) from exc
            if not present:
                raise AuthorityRefusal(AUTHORITY_NOT_READY)
            try:
                note = company_scope.gate_note(
                    conn, action, argv, "ACTIVE")
            except company_scope.ScopeRefused as exc:
                raise AuthorityRefusal(exc.code) from exc
            except AuthorityRefusal:
                raise
            except Exception as exc:
                raise AuthorityRefusal(
                    company_scope.REFUSAL_CODE) from exc
        finally:
            try:
                conn.rollback()
            except Exception as exc:
                raise AuthorityRefusal(
                    company_scope.REFUSAL_CODE) from exc
        return note
    try:
        try:
            note = company_scope.gate_note(conn, action, argv, "STAGED")
        except Exception:
            note = company_scope.Note(None, company_scope.UNDERIVED)
    finally:
        try:
            conn.rollback()
        except Exception:
            note = company_scope.Note(None, company_scope.UNDERIVED)
    return note


def run(conn, action, argv, handler, *, option_strings=None, repeatable_options=()):
    """Gate one domain action behind single-use authorization."""
    _rest, authorization_id = split_authorization_id(argv)
    if authorization_id is not None:
        if option_strings is None:
            raise ValueError(INPUT_INVALID)
        allowed = set(option_strings)
        if repeatable_options is None:
            repeatable = set()
        else:
            repeatable = set(repeatable_options)
        seen = {}
        for token in _rest:
            if token.startswith("-"):
                name = token.split("=", 1)[0]
                if name not in allowed:
                    raise ValueError(INPUT_INVALID)
                count = seen.get(name, 0) + 1
                seen[name] = count
                if count > 1 and name not in repeatable:
                    raise ValueError(INPUT_INVALID)
    phase, _install = install_phase(conn)
    conn.rollback()
    decl = action_impact.declaration(action)
    if authorization_id is not None:
        if decl is None:
            raise AuthorityRefusal(IMPACT_UNDECLARED)
        if not decl.get("enveloped"):
            raise AuthorityRefusal(AUTHORIZATION_REFUSED)
        note = _scope_step(conn, action, _rest, phase)
        try:
            token = company_scope.bind_note(conn, note)
        except Exception as exc:
            if phase == "ACTIVE":
                raise AuthorityRefusal(
                    company_scope.REFUSAL_CODE) from exc
            try:
                stale = company_scope.bound_note(conn)
            except Exception:
                stale = None
            if stale is not None:
                try:
                    company_scope._clear_note(conn)
                    retry_token = company_scope.bind_note(conn, note)
                except Exception:
                    return verify_and_consume(
                        conn, authorization_id=authorization_id,
                        action=action, argv=argv, handler=handler)
                try:
                    return verify_and_consume(
                        conn, authorization_id=authorization_id,
                        action=action, argv=argv, handler=handler)
                finally:
                    try:
                        company_scope.unbind_note(conn, retry_token)
                    except Exception:
                        pass
            return verify_and_consume(
                conn, authorization_id=authorization_id,
                action=action, argv=argv, handler=handler)
        failed = False
        try:
            return verify_and_consume(
                conn, authorization_id=authorization_id,
                action=action, argv=argv, handler=handler)
        except SystemExit as exc:
            failed = exc.code not in (None, 0)
            raise
        except BaseException:
            failed = True
            raise
        finally:
            try:
                company_scope.unbind_note(conn, token)
            except Exception:
                if phase == "ACTIVE" and not failed:
                    raise
    if phase == "ACTIVE":
        if not authority_readiness.is_ready(conn):
            raise AuthorityRefusal(AUTHORITY_NOT_READY)
        if decl is None:
            raise AuthorityRefusal(IMPACT_UNDECLARED)
        if decl.get("enveloped"):
            if action not in ENVELOPE_ACTIONS:
                raise AuthorityRefusal(IMPACT_UNDECLARED)
            raise AuthorityRefusal(AUTHORIZATION_REQUIRED)
        note = _scope_step(conn, action, _rest, phase)
        try:
            token = company_scope.bind_note(conn, note)
        except Exception as exc:
            raise AuthorityRefusal(
                company_scope.REFUSAL_CODE) from exc
        failed = False
        try:
            return handler(conn)
        except SystemExit as exc:
            failed = exc.code not in (None, 0)
            raise
        except BaseException:
            failed = True
            raise
        finally:
            try:
                company_scope.unbind_note(conn, token)
            except Exception:
                if phase == "ACTIVE" and not failed:
                    raise
    if decl is None:
        _LOG.warning("action %s: impact undeclared", action)
    note = _scope_step(conn, action, _rest, phase)
    try:
        token = company_scope.bind_note(conn, note)
    except Exception:
        try:
            stale = company_scope.bound_note(conn)
        except Exception:
            stale = None
        if stale is not None:
            try:
                company_scope._clear_note(conn)
                retry_token = company_scope.bind_note(conn, note)
            except Exception:
                return handler(conn)
            try:
                return handler(conn)
            finally:
                try:
                    company_scope.unbind_note(conn, retry_token)
                except Exception:
                    pass
        return handler(conn)
    try:
        return handler(conn)
    finally:
        try:
            company_scope.unbind_note(conn, token)
        except Exception:
            pass
