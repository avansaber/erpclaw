"""Issue, revoke and read single-use authorization envelopes."""

import re
import uuid
from decimal import Decimal

from erpclaw_lib import actor
from erpclaw_lib import authority_clock
from erpclaw_lib import authority_readiness
from erpclaw_lib import company_scope
from erpclaw_lib.audit import audit as _audit
from erpclaw_lib.authority_clock import (
    EXACT_APPROVAL_DEFAULT_MS,
    EXACT_APPROVAL_MAX_MS,
    ROUTINE_MAX_MS,
)
from erpclaw_lib.authority_gate import (
    AUTHORITY_NOT_READY,
    IMPACT_UNDECLARED,
    AuthorityRefusal,
    ENVELOPE_ACTIONS,
    install_phase,
    open_read_transaction,
    open_transaction,
    rights_hold,
)
from erpclaw_lib.authorization_consumption import (
    INPUT_INVALID,
    _ACTION_RE,
    _ID_RE,
    _check_amount_value,
    _checked_wrapper,
    canonical_binding_digest,
)
from erpclaw_lib.authorization_envelope import (
    ENVELOPE_VERSION,
    args_digest,
    canonical_pairs,
    envelope_digest,
)
from erpclaw_lib.db import db_error_types, integrity_error_types
from erpclaw_lib.query import Field, P, Q, Table

AUTHORIZATION_ISSUANCE_REFUSED = "AUTHORIZATION_ISSUANCE_REFUSED"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
AUTHORIZATION_ISSUER_UNAVAILABLE = "AUTHORIZATION_ISSUER_UNAVAILABLE"

_REASON_CODE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")

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


def _need_id(value):
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise ValueError(INPUT_INVALID)
    return value


def _need_action(value):
    if type(value) is not str:
        raise ValueError(INPUT_INVALID)
    if _ACTION_RE.fullmatch(value) is None:
        raise ValueError(INPUT_INVALID)
    return value


def _need_reason_code(value):
    if type(value) is not str:
        raise ValueError(INPUT_INVALID)
    if _REASON_CODE_RE.fullmatch(value) is None:
        raise ValueError(INPUT_INVALID)


def _need_reason_text(value):
    if type(value) is not str or not 1 <= len(value) <= 280:
        raise ValueError(INPUT_INVALID)


def _need_call_id(value):
    if value is not None:
        _need_id(value)


def _need_argv(argv):
    if type(argv) is not list:
        raise ValueError(INPUT_INVALID)
    for item in argv:
        if type(item) is not str:
            raise ValueError(INPUT_INVALID)


def _need_lifetime(value, maximum):
    if value is None:
        return maximum
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(INPUT_INVALID)
    return value


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
    missing, _base = db_error_types()
    try:
        rows = _rows_where(conn, table_name, columns, filters)
    except missing:
        return None
    if not rows:
        return None
    return rows[0]


def _read_delegation(conn, install_id, delegation_id):
    return _single_row(
        conn, "authority_delegation",
        ["issuer_id", "grantee_id", "issued_at", "expires_at",
         "revoked_at"],
        [("install_id", install_id), ("id", delegation_id)])


def _read_principal(conn, install_id, principal_id):
    return _single_row(
        conn, "authority_principal", ["kind", "disabled_at"],
        [("install_id", install_id), ("id", principal_id)])


def _key_rows(conn, install_id, principal_id, idempotency_key):
    side = _single_row(
        conn, "operation_authorization_envelope", _SIDE_COLS,
        [("install_id", install_id),
         ("principal_id", principal_id),
         ("idempotency_key", idempotency_key)])
    if side is None:
        return None
    auth = _single_row(conn, "operation_authorization",
                       _AUTH_COLS,
                       [("id", side["authorization_id"])])
    if auth is None:
        return (None, side)
    return (auth, side)


def _key_matches(auth, side, action, delegation_id, digest,
                 issuer_id, reason_code, reason_text, call_id):
    if auth is None:
        return False
    return (
        auth["action"] == action
        and auth["delegation_id"] == delegation_id
        and side["args_digest"] == digest
        and side["issuer_id"] == issuer_id
        and side["reason_code"] == reason_code
        and side["reason_text"] == reason_text
        and side["call_id"] == call_id
    )


def _key_return(auth, side):
    return {
        "authorization_id": side["authorization_id"],
        "expires_at": auth["expires_at"],
        "issued_route": side["issued_route"],
        "envelope_digest": side["envelope_digest"],
        "idempotent": True,
    }


def _find_cap_row(conn, install_id, delegation_id, action,
                  currency, now):
    rows = _rows_where(
        conn, "authority_delegation_cap",
        ["install_id", "delegation_id", "action", "currency",
         "scale", "per_operation", "aggregate_limit",
         "window_start", "window_end"],
        [("install_id", install_id),
         ("delegation_id", delegation_id),
         ("action", action), ("currency", currency)])
    try:
        live = [row for row in rows
                if row["window_start"] <= now < row["window_end"]]
    except (KeyError, TypeError):
        return None
    if len(live) != 1:
        return None
    return live[0]


def _usage_text(conn, cap):
    row = _single_row(
        conn, "authority_delegation_usage", ["used"],
        [("install_id", cap["install_id"]),
         ("delegation_id", cap["delegation_id"]),
         ("action", cap["action"]),
         ("currency", cap["currency"]),
         ("window_start", cap["window_start"]),
         ("window_end", cap["window_end"])])
    if row is None:
        return None
    return row["used"]


def _zero_text(scale):
    if scale == 0:
        return "0"
    return "0." + "0" * scale


def _check_cap(conn, install_id, delegation_id, action, amount,
               now):
    try:
        currency = amount["currency"]
        value = amount["value"]
        scale = amount["scale"]
    except (KeyError, TypeError):
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    cap = _find_cap_row(conn, install_id, delegation_id,
                        action, currency, now)
    if cap is None or cap.get("scale") != scale:
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    try:
        _check_amount_value(value, scale)
        current = _usage_text(conn, cap)
        if current is None:
            current = _zero_text(scale)
        _check_amount_value(current, scale)
        over_single = (Decimal(value)
                       > Decimal(cap["per_operation"]))
        over_total = (Decimal(current) + Decimal(value)
                      > Decimal(cap["aggregate_limit"]))
    except Exception:
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    if over_single or over_total:
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    return cap


def _issuer_scope_holds(conn, install_id, issuer_id, company_ids):
    issuer_scope = company_scope.principal_scope(conn, install_id, issuer_id)
    if not company_ids:
        return False
    for entry in company_ids:
        if entry not in issuer_scope:
            return False
    return True


def _derive_checked(declaration, conn, action, pairs):
    try:
        derived = declaration["derive"](conn, action, pairs)
        company_ids = derived["company_ids"]
        targets = derived["targets"]
        amounts = derived["amounts"]
    except AuthorityRefusal:
        raise
    except Exception:
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    if (type(company_ids) is not list or len(company_ids) != 1
            or type(company_ids[0]) is not str):
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    if type(targets) is not list or type(amounts) is not list:
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    return company_ids, targets, amounts


def _declaration_keywords(declaration):
    return {
        "money": declaration.get("money", ()),
        "json_args": declaration.get("json_args", ()),
        "money_json_paths": declaration.get("money_json_paths"),
        "bound_args": declaration.get("bound_args", ()),
    }


def _insert_auth(conn, auth_id, install_id, principal_id, action,
                 binding_digest, delegation_id, issued_at,
                 expires_at):
    table = Table("operation_authorization")
    query = Q.into(table).columns(
        "id", "install_id", "principal_id", "action",
        "binding_digest", "delegation_id", "issued_at",
        "expires_at", "revoked_at", "consumed_at",
        "consumed_txn").insert(
        P(), P(), P(), P(), P(), P(), P(), P(), P(), P(),
        P()).get_sql()
    conn.execute(query, (auth_id, install_id, principal_id,
                         action, binding_digest, delegation_id,
                         issued_at, expires_at, None, None,
                         None))


def _insert_side(conn, auth_id, install_id, principal_id, digest,
                 issuer_id, issued_route, reason_code,
                 reason_text, idempotency_key, call_id,
                 env_digest):
    table = Table("operation_authorization_envelope")
    query = Q.into(table).columns(
        "authorization_id", "install_id", "principal_id",
        "envelope_version", "args_digest", "issuer_id",
        "issued_route", "reason_code", "reason_text",
        "idempotency_key", "call_id",
        "envelope_digest").insert(
        P(), P(), P(), P(), P(), P(), P(), P(), P(), P(),
        P(), P()).get_sql()
    conn.execute(query, (auth_id, install_id, principal_id,
                         ENVELOPE_VERSION, digest, issuer_id,
                         issued_route, reason_code, reason_text,
                         idempotency_key, call_id, env_digest))


def _ensure_usage(conn, cap):
    if _usage_text(conn, cap) is not None:
        return
    table = Table("authority_delegation_usage")
    query = Q.into(table).columns(
        "install_id", "delegation_id", "action", "currency",
        "window_start", "window_end", "used").insert(
        P(), P(), P(), P(), P(), P(), P()).get_sql()
    conn.execute(query, (cap["install_id"],
                         cap["delegation_id"], cap["action"],
                         cap["currency"], cap["window_start"],
                         cap["window_end"],
                         _zero_text(cap["scale"])))


def _issue_routine_once(conn, *, principal_id, delegation_id,
                        action, argv, reason_code, reason_text,
                        idempotency_key, call_id, lifetime_ms):
    open_transaction(conn)
    try:
        now = authority_clock.now_ms()
        phase, install_id = install_phase(conn)
        _checked_wrapper(conn)
        live = phase == "ACTIVE"
        if live:
            if not authority_readiness.is_ready(conn):
                raise AuthorityRefusal(AUTHORITY_NOT_READY)
            current = actor.current()
            if (current.status != actor.ATTESTED
                    or current.principal_claim != principal_id):
                raise AuthorityRefusal(
                    AUTHORIZATION_ISSUANCE_REFUSED)
        declaration = ENVELOPE_ACTIONS.get(action)
        if declaration is None:
            raise AuthorityRefusal(IMPACT_UNDECLARED)
        if declaration.get("class") != "transaction":
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        keywords = _declaration_keywords(declaration)
        pairs = canonical_pairs(argv, **keywords)
        digest = args_digest(argv, **keywords)
        delegation = _read_delegation(conn, install_id,
                                      delegation_id)
        if delegation is None:
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        issuer_id = delegation["issuer_id"]
        found = _key_rows(conn, install_id, principal_id,
                          idempotency_key)
        if found is not None:
            auth_hit, side_hit = found
            if _key_matches(auth_hit, side_hit, action,
                            delegation_id, digest, issuer_id,
                            reason_code, reason_text, call_id):
                try:
                    conn.rollback()
                except Exception:
                    pass
                return _key_return(auth_hit, side_hit)
            raise AuthorityRefusal(IDEMPOTENCY_CONFLICT)
        company_ids, targets, amounts = _derive_checked(
            declaration, conn, action, pairs)
        if live:
            try:
                holds = _issuer_scope_holds(
                    conn, install_id, issuer_id, company_ids)
            except AuthorityRefusal:
                raise
            except Exception:
                raise AuthorityRefusal(
                    AUTHORIZATION_ISSUANCE_REFUSED)
            if not holds:
                raise AuthorityRefusal(
                    AUTHORIZATION_ISSUANCE_REFUSED)
        if not rights_hold(conn, install_id, principal_id,
                           delegation_id, company_ids[0],
                           targets, action, now):
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        caps = [_check_cap(conn, install_id, delegation_id,
                           action, amount, now)
                for amount in amounts]
        lifetime = _need_lifetime(lifetime_ms, ROUTINE_MAX_MS)
        issued_at = now
        expires_at = issued_at + lifetime
        if delegation["expires_at"] < expires_at:
            expires_at = delegation["expires_at"]
        issued_route = "delegation" if live else "staged_unattested"
        binding = {
            "version": 1,
            "install_id": install_id,
            "principal_id": principal_id,
            "delegation_id": delegation_id,
            "action": action,
            "company_ids": company_ids,
            "targets": targets,
            "amounts": amounts,
            "expires_at": expires_at,
        }
        try:
            binding_digest = canonical_binding_digest(binding)
        except ValueError:
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        auth_id = str(uuid.uuid4())
        try:
            env_digest = envelope_digest({
                "v": ENVELOPE_VERSION,
                "binding_digest": binding_digest,
                "args_digest": digest,
                "issuer_id": issuer_id,
                "issued_route": issued_route,
                "reason_code": reason_code,
                "reason_text": reason_text,
                "idempotency_key": idempotency_key,
                "call_id": call_id,
                "issued_at": issued_at,
            })
        except ValueError:
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        _insert_auth(conn, auth_id, install_id, principal_id,
                     action, binding_digest, delegation_id,
                     issued_at, expires_at)
        _insert_side(conn, auth_id, install_id, principal_id,
                     digest, issuer_id, issued_route,
                     reason_code, reason_text, idempotency_key,
                     call_id, env_digest)
        for cap in caps:
            _ensure_usage(conn, cap)
        conn.commit()
        return {
            "authorization_id": auth_id,
            "expires_at": expires_at,
            "issued_route": issued_route,
            "envelope_digest": env_digest,
            "idempotent": False,
        }
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def issue_envelope(conn, *, principal_id, delegation_id, action,
                   argv, reason_code, reason_text,
                   idempotency_key, call_id=None,
                   lifetime_ms=None):
    """Mint one routine envelope under a human-issued delegation."""
    _need_id(principal_id)
    _need_id(delegation_id)
    _need_action(action)
    _need_argv(argv)
    _need_reason_code(reason_code)
    _need_reason_text(reason_text)
    _need_id(idempotency_key)
    _need_call_id(call_id)
    try:
        return _issue_routine_once(
            conn, principal_id=principal_id,
            delegation_id=delegation_id, action=action,
            argv=argv, reason_code=reason_code,
            reason_text=reason_text,
            idempotency_key=idempotency_key, call_id=call_id,
            lifetime_ms=lifetime_ms)
    except integrity_error_types():
        pass
    try:
        return _issue_routine_once(
            conn, principal_id=principal_id,
            delegation_id=delegation_id, action=action,
            argv=argv, reason_code=reason_code,
            reason_text=reason_text,
            idempotency_key=idempotency_key, call_id=call_id,
            lifetime_ms=lifetime_ms)
    except integrity_error_types():
        pass
    open_transaction(conn)
    try:
        _phase, install_id = install_phase(conn)
        _checked_wrapper(conn)
        found = _key_rows(conn, install_id, principal_id,
                          idempotency_key)
        try:
            conn.rollback()
        except Exception:
            pass
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    if found is not None:
        raise AuthorityRefusal(IDEMPOTENCY_CONFLICT)
    raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)


def _issue_exact_once(conn, *, issuer_id, principal_id, action,
                      argv, reason_code, reason_text,
                      idempotency_key, call_id, lifetime_ms):
    open_transaction(conn)
    try:
        now = authority_clock.now_ms()
        phase, install_id = install_phase(conn)
        _checked_wrapper(conn)
        if phase == "ACTIVE":
            raise AuthorityRefusal(
                AUTHORIZATION_ISSUER_UNAVAILABLE)
        if issuer_id == principal_id:
            raise AuthorityRefusal(
                AUTHORIZATION_ISSUANCE_REFUSED)
        issuer = _read_principal(conn, install_id, issuer_id)
        if issuer is None or issuer["kind"] != "human":
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        if issuer["disabled_at"] is not None:
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        declaration = ENVELOPE_ACTIONS.get(action)
        if declaration is None:
            raise AuthorityRefusal(IMPACT_UNDECLARED)
        if declaration.get("class") not in ("transaction",
                                            "sensitive"):
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        keywords = _declaration_keywords(declaration)
        pairs = canonical_pairs(argv, **keywords)
        digest = args_digest(argv, **keywords)
        found = _key_rows(conn, install_id, principal_id,
                          idempotency_key)
        if found is not None:
            auth_hit, side_hit = found
            if _key_matches(auth_hit, side_hit, action, None,
                            digest, issuer_id, reason_code,
                            reason_text, call_id):
                try:
                    conn.rollback()
                except Exception:
                    pass
                return _key_return(auth_hit, side_hit)
            raise AuthorityRefusal(IDEMPOTENCY_CONFLICT)
        company_ids, targets, amounts = _derive_checked(
            declaration, conn, action, pairs)
        if not rights_hold(conn, install_id, principal_id,
                           None, company_ids[0], targets,
                           action, now):
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        if lifetime_ms is None:
            lifetime = EXACT_APPROVAL_DEFAULT_MS
        elif (type(lifetime_ms) is not int
                or not 1 <= lifetime_ms <= EXACT_APPROVAL_MAX_MS):
            raise ValueError(INPUT_INVALID)
        else:
            lifetime = lifetime_ms
        issued_at = now
        expires_at = issued_at + lifetime
        binding = {
            "version": 1,
            "install_id": install_id,
            "principal_id": principal_id,
            "delegation_id": None,
            "action": action,
            "company_ids": company_ids,
            "targets": targets,
            "amounts": amounts,
            "expires_at": expires_at,
        }
        try:
            binding_digest = canonical_binding_digest(binding)
        except ValueError:
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        auth_id = str(uuid.uuid4())
        try:
            env_digest = envelope_digest({
                "v": ENVELOPE_VERSION,
                "binding_digest": binding_digest,
                "args_digest": digest,
                "issuer_id": issuer_id,
                "issued_route": "staged_unattested",
                "reason_code": reason_code,
                "reason_text": reason_text,
                "idempotency_key": idempotency_key,
                "call_id": call_id,
                "issued_at": issued_at,
            })
        except ValueError:
            raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
        _insert_auth(conn, auth_id, install_id, principal_id,
                     action, binding_digest, None, issued_at,
                     expires_at)
        _insert_side(conn, auth_id, install_id, principal_id,
                     digest, issuer_id, "staged_unattested",
                     reason_code, reason_text, idempotency_key,
                     call_id, env_digest)
        conn.commit()
        return {
            "authorization_id": auth_id,
            "expires_at": expires_at,
            "issued_route": "staged_unattested",
            "envelope_digest": env_digest,
            "idempotent": False,
        }
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def issue_exact_approval(conn, *, issuer_id, principal_id,
                         action, argv, reason_code, reason_text,
                         idempotency_key, call_id=None,
                         lifetime_ms=None):
    """Mint one exact-approval envelope while the install is staged."""
    _need_id(issuer_id)
    _need_id(principal_id)
    _need_action(action)
    _need_argv(argv)
    _need_reason_code(reason_code)
    _need_reason_text(reason_text)
    _need_id(idempotency_key)
    _need_call_id(call_id)
    if (lifetime_ms is not None
            and (type(lifetime_ms) is not int
                 or lifetime_ms < 1
                 or lifetime_ms > EXACT_APPROVAL_MAX_MS)):
        raise ValueError(INPUT_INVALID)
    try:
        return _issue_exact_once(
            conn, issuer_id=issuer_id,
            principal_id=principal_id, action=action,
            argv=argv, reason_code=reason_code,
            reason_text=reason_text,
            idempotency_key=idempotency_key, call_id=call_id,
            lifetime_ms=lifetime_ms)
    except integrity_error_types():
        pass
    try:
        return _issue_exact_once(
            conn, issuer_id=issuer_id,
            principal_id=principal_id, action=action,
            argv=argv, reason_code=reason_code,
            reason_text=reason_text,
            idempotency_key=idempotency_key, call_id=call_id,
            lifetime_ms=lifetime_ms)
    except integrity_error_types():
        pass
    open_transaction(conn)
    try:
        _phase, install_id = install_phase(conn)
        _checked_wrapper(conn)
        found = _key_rows(conn, install_id, principal_id,
                          idempotency_key)
        try:
            conn.rollback()
        except Exception:
            pass
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)
    if found is not None:
        raise AuthorityRefusal(IDEMPOTENCY_CONFLICT)
    raise AuthorityRefusal(AUTHORIZATION_ISSUANCE_REFUSED)


def revoke_envelope(conn, *, authorization_id):
    """Mark one envelope revoked while the install is staged."""
    _need_id(authorization_id)
    open_transaction(conn)
    try:
        phase, _install_id = install_phase(conn)
        _checked_wrapper(conn)
        if phase == "ACTIVE":
            raise AuthorityRefusal(
                AUTHORIZATION_ISSUER_UNAVAILABLE)
        now = authority_clock.now_ms()
        table = Table("operation_authorization")
        query = Q.update(table).set(
            Field("revoked_at"), P()).where(
            Field("id") == P()).where(
            Field("revoked_at").isnull()).where(
            Field("consumed_at").isnull()).get_sql()
        cursor = conn.execute(query, (now, authorization_id))
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
            raise AuthorityRefusal(
                "AUTHORIZATION_REFUSED")
        _audit(conn, "erpclaw-setup", "revoke-authorization",
               "operation_authorization", authorization_id,
               authorization_id=authorization_id,
               authorization_status=None)
        conn.commit()
        return {"authorization_id": authorization_id,
                "revoked": True}
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def get_envelope(conn, *, authorization_id):
    """Read one envelope and its stored result, if any."""
    _need_id(authorization_id)
    open_read_transaction(conn)
    try:
        phase, _install_id = install_phase(conn)
        _checked_wrapper(conn)
        if phase == "ACTIVE":
            raise AuthorityRefusal(
                AUTHORIZATION_ISSUER_UNAVAILABLE)
        auth = _single_row(conn, "operation_authorization",
                           _AUTH_COLS,
                           [("id", authorization_id)])
        side = _single_row(
            conn, "operation_authorization_envelope",
            _SIDE_COLS,
            [("authorization_id", authorization_id)])
        if auth is None or side is None:
            raise AuthorityRefusal("AUTHORIZATION_REFUSED")
        done = _single_row(
            conn, "operation_authorization_result",
            _RESULT_COLS,
            [("authorization_id", authorization_id)])
        try:
            conn.rollback()
        except Exception:
            pass
        out = {
            "id": authorization_id,
            "authorization_id": authorization_id,
            "action": auth["action"],
            "principal_id": auth["principal_id"],
            "delegation_id": auth["delegation_id"],
            "issued_at": auth["issued_at"],
            "expires_at": auth["expires_at"],
            "revoked": auth["revoked_at"] is not None,
            "consumed": (auth["consumed_at"] is not None
                         or auth["consumed_txn"] is not None),
            "issued_route": side["issued_route"],
            "reason_code": side["reason_code"],
            "envelope_digest": side["envelope_digest"],
        }
        if done is not None:
            out["result_kind"] = done["result_kind"]
            out["result_id"] = done["result_id"]
            out["result_status"] = done["result_status"]
        return out
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
