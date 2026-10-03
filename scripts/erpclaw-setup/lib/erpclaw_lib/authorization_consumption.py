"""Connection-bound single-use authorization consumption primitive.

This module exposes one unwired internal helper that flips a previously
issued authorization row to consumed inside the caller's own transaction,
plus a pure digest helper for the bound payload.

Scope limits, read before use:
- The primitive performs no permission evaluation, no principal
  authentication, and no approval issuance. Inputs arrive from trusted
  service code; later reviewed callers own provenance, live permission
  re-checks, target-state derivation, and execution-time expiry handling.
- A CONSUMED return is provisional until the caller commits. REFUSED
  merges every ordinary mismatch, missing, revoked, expired, or
  already-consumed case into one answer with no row data and no
  existence signal.
- The caller owns rollback on every raised exception. The primitive
  never commits, rolls back, opens a connection, or touches the catalog.
"""

import hashlib
import json
import re

from erpclaw_lib.db import ConnectionWrapper, PgConnectionWrapper
from erpclaw_lib.query import Field, P, Q, Table

INPUT_INVALID = "AUTHORIZATION_INPUT_INVALID"
STORAGE_ERROR = "AUTHORIZATION_STORAGE_ERROR"
STORAGE_INVALID = "AUTHORIZATION_STORAGE_INVALID"

CONSUMED = "CONSUMED"
REFUSED = "REFUSED"

_INT_MAX = 9223372036854775807
_AUTH_TABLE = "operation_authorization"

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_ACTION_RE = re.compile(r"[a-z][a-z0-9-]{0,127}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_INT_PART_RE = re.compile(r"(0|[1-9][0-9]*)")
_UINT_PART_RE = re.compile(r"[0-9]+")

_BINDING_KEYS = frozenset((
    "version", "install_id", "principal_id", "delegation_id", "action",
    "company_ids", "targets", "amounts", "expires_at",
))
_TARGET_KEYS = frozenset(("kind", "id", "state_digest"))
_AMOUNT_KEYS = frozenset(("currency", "value", "scale"))


def _fail():
    raise ValueError(INPUT_INVALID) from None


def _check_keys(mapping, expected):
    if type(mapping) is not dict:
        _fail()
    try:
        keys = list(mapping.keys())
    except Exception:
        raise ValueError(INPUT_INVALID) from None
    for key in keys:
        if type(key) is not str:
            _fail()
    if set(keys) != expected:
        _fail()


def _check_id(value):
    if type(value) is not str:
        _fail()
    if _ID_RE.fullmatch(value) is None:
        _fail()


def _check_action(value):
    if type(value) is not str:
        _fail()
    if _ACTION_RE.fullmatch(value) is None:
        _fail()


def _check_epoch(value):
    if type(value) is not int:
        _fail()
    if value < 0 or value > _INT_MAX:
        _fail()


def _check_amount_value(value, scale):
    if type(value) is not str:
        _fail()
    if len(value) == 0 or len(value) > 128:
        _fail()
    text = value
    negative = False
    if text[0] == "-":
        negative = True
        text = text[1:]
        if len(text) == 0:
            _fail()
    if scale == 0:
        if _INT_PART_RE.fullmatch(text) is None:
            _fail()
        if negative and text == "0":
            _fail()
        return
    parts = text.split(".")
    if len(parts) != 2:
        _fail()
    int_part, frac_part = parts
    if len(frac_part) != scale:
        _fail()
    if _INT_PART_RE.fullmatch(int_part) is None:
        _fail()
    if _UINT_PART_RE.fullmatch(frac_part) is None:
        _fail()
    if negative and int_part == "0" and frac_part.strip("0") == "":
        _fail()


def _canonical_form(binding):
    _check_keys(binding, _BINDING_KEYS)
    version = binding["version"]
    if type(version) is not int or version != 1:
        _fail()
    install_id = binding["install_id"]
    principal_id = binding["principal_id"]
    _check_id(install_id)
    _check_id(principal_id)
    delegation_id = binding["delegation_id"]
    if delegation_id is not None:
        _check_id(delegation_id)
    action = binding["action"]
    _check_action(action)
    company_ids = binding["company_ids"]
    if type(company_ids) is not list or not 1 <= len(company_ids) <= 64:
        _fail()
    for entry in company_ids:
        _check_id(entry)
    if len(set(company_ids)) != len(company_ids):
        _fail()
    targets = binding["targets"]
    if type(targets) is not list or not 1 <= len(targets) <= 128:
        _fail()
    seen = set()
    norm_targets = []
    for item in targets:
        _check_keys(item, _TARGET_KEYS)
        kind = item["kind"]
        target_id = item["id"]
        state_digest = item["state_digest"]
        _check_action(kind)
        _check_id(target_id)
        if type(state_digest) is not str:
            _fail()
        if _HEX64_RE.fullmatch(state_digest) is None:
            _fail()
        key = (kind, target_id)
        if key in seen:
            _fail()
        seen.add(key)
        norm_targets.append({
            "id": target_id,
            "kind": kind,
            "state_digest": state_digest,
        })
    norm_targets.sort(key=lambda entry: (entry["kind"], entry["id"]))
    amounts = binding["amounts"]
    if type(amounts) is not list or not 0 <= len(amounts) <= 32:
        _fail()
    norm_amounts = []
    for item in amounts:
        _check_keys(item, _AMOUNT_KEYS)
        currency = item["currency"]
        value = item["value"]
        scale = item["scale"]
        if type(currency) is not str:
            _fail()
        if _CURRENCY_RE.fullmatch(currency) is None:
            _fail()
        if type(scale) is not int or scale < 0 or scale > 18:
            _fail()
        _check_amount_value(value, scale)
        norm_amounts.append({
            "currency": currency,
            "scale": scale,
            "value": value,
        })
    norm_amounts.sort(
        key=lambda entry: (entry["currency"], entry["scale"], entry["value"]))
    expires_at = binding["expires_at"]
    _check_epoch(expires_at)
    return {
        "action": action,
        "amounts": norm_amounts,
        "company_ids": sorted(company_ids),
        "delegation_id": delegation_id,
        "expires_at": expires_at,
        "install_id": install_id,
        "principal_id": principal_id,
        "targets": norm_targets,
        "version": 1,
    }


def _digest_norm(norm):
    text = json.dumps(
        norm,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_binding_digest(binding):
    norm = _canonical_form(binding)
    return _digest_norm(norm)


def _checked_wrapper(conn):
    try:
        raw = object.__getattribute__(conn, "_conn")
    except Exception:
        _fail()
    if isinstance(conn, PgConnectionWrapper):
        try:
            auto = raw.autocommit
        except Exception:
            _fail()
        if auto:
            _fail()
        try:
            from psycopg2.extensions import (
                TRANSACTION_STATUS_INTRANS as _intrans,
            )
        except Exception:
            raise ValueError(INPUT_INVALID) from None
        try:
            status = raw.get_transaction_status()
        except Exception:
            _fail()
        if status != _intrans:
            _fail()
        return
    if isinstance(conn, ConnectionWrapper):
        try:
            in_txn = raw.in_transaction
        except Exception:
            _fail()
        if not in_txn:
            _fail()
        try:
            isolation = raw.isolation_level
        except Exception:
            _fail()
        if isolation is None:
            _fail()
        return
    _fail()


def consume(conn, *, authorization_id, binding, now, txn_id):
    if type(authorization_id) is not str:
        raise ValueError(INPUT_INVALID)
    if _ID_RE.fullmatch(authorization_id) is None:
        raise ValueError(INPUT_INVALID)
    if type(txn_id) is not str:
        raise ValueError(INPUT_INVALID)
    if _ID_RE.fullmatch(txn_id) is None:
        raise ValueError(INPUT_INVALID)
    if type(now) is not int:
        raise ValueError(INPUT_INVALID)
    if now < 0 or now > _INT_MAX:
        raise ValueError(INPUT_INVALID)
    norm = _canonical_form(binding)
    digest = _digest_norm(norm)
    _checked_wrapper(conn)
    table = Table(_AUTH_TABLE)
    query = Q.update(table)
    query = query.set(Field("consumed_at"), P())
    query = query.set(Field("consumed_txn"), P())
    query = query.where(Field("id") == P())
    query = query.where(Field("install_id") == P())
    query = query.where(Field("principal_id") == P())
    query = query.where(Field("action") == P())
    delegation_id = norm["delegation_id"]
    if delegation_id is None:
        query = query.where(Field("delegation_id").isnull())
    else:
        query = query.where(Field("delegation_id") == P())
    query = query.where(Field("binding_digest") == P())
    query = query.where(Field("expires_at") == P())
    query = query.where(Field("issued_at") <= P())
    query = query.where(Field("expires_at") > P())
    query = query.where(Field("revoked_at").isnull())
    query = query.where(Field("consumed_at").isnull())
    query = query.where(Field("consumed_txn").isnull())
    sql = query.get_sql()
    params = [
        now,
        txn_id,
        authorization_id,
        norm["install_id"],
        norm["principal_id"],
        norm["action"],
    ]
    if delegation_id is not None:
        params.append(delegation_id)
    params.extend([digest, norm["expires_at"], now, now])
    try:
        cursor = conn.execute(sql, params)
    except Exception:
        raise RuntimeError(STORAGE_ERROR) from None
    try:
        count = cursor.rowcount
    except Exception:
        try:
            cursor.close()
        except Exception:
            pass
        raise RuntimeError(STORAGE_ERROR) from None
    try:
        cursor.close()
    except Exception:
        raise RuntimeError(STORAGE_ERROR) from None
    if count == 1:
        return CONSUMED
    if count == 0:
        return REFUSED
    raise RuntimeError(STORAGE_INVALID)
