"""Canonical form and digest for single-use authorization envelopes.

Format only: nothing here issues, verifies or consumes an envelope. The
functions below turn an argv-style argument list into one canonical pair
list and one digest, and seal a fixed envelope field set into one versioned
digest. Any field added, removed or re-normalised is a new ``v``; verifiers
accept only versions they implement.
"""

import hashlib
import json
import re
from decimal import Decimal

from erpclaw_lib.authorization_consumption import (
    INPUT_INVALID,
    _HEX64_RE,
    _ID_RE,
    _INT_MAX,
)

ENVELOPE_VERSION = 2
ISSUED_ROUTES = ("delegation", "exact_approval", "staged_unattested")
EXCLUDED_NAMES = (
    "action",
    "db-path",
    "db-url",
    "user-confirmed",
    "authorization-id",
)
AUDIT_STATUSES = ("absent", "verified", "staged_unattested", "refused")
REASON_TEXT_MAX = 280
_REASON_CODE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_MONEY_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")

_ENVELOPE_KEYS = frozenset((
    "v",
    "binding_digest",
    "args_digest",
    "issuer_id",
    "issued_route",
    "reason_code",
    "reason_text",
    "idempotency_key",
    "call_id",
    "issued_at",
))


def _fail():
    raise ValueError(INPUT_INVALID) from None


def _normalise_name(name):
    return name.replace("_", "-")


def normalise_money(text):
    if type(text) is not str:
        _fail()
    if _MONEY_RE.fullmatch(text) is None:
        _fail()
    out = format(Decimal(text), "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    if out == "-0":
        out = "0"
    return out


def _refuse_constant(value):
    _fail()


def _refuse_duplicate_keys(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            _fail()
        seen.add(key)
    return dict(pairs)


def _money_json_text(value):
    if value is True or value is False or value is None:
        _fail()
    if isinstance(value, Decimal):
        text = str(value)
    elif type(value) is int:
        text = str(value)
    elif type(value) is str:
        text = value
    else:
        _fail()
    return json.dumps(normalise_money(text), ensure_ascii=True)


def _canonical_json_text(value, money_paths):
    def _render(node, trail):
        if type(node) is dict:
            parts = []
            for key in sorted(node.keys()):
                child = trail + (key,)
                if child in money_paths:
                    rendered = _money_json_text(node[key])
                else:
                    rendered = _render(node[key], child)
                parts.append(
                    json.dumps(key, ensure_ascii=True) + ":" + rendered)
            return "{" + ",".join(parts) + "}"
        if type(node) is list:
            child = trail + ("*",)
            if child in money_paths:
                return "[" + ",".join(
                    _money_json_text(item) for item in node) + "]"
            return "[" + ",".join(
                _render(item, child) for item in node) + "]"
        if node is True:
            return "true"
        if node is False:
            return "false"
        if node is None:
            return "null"
        if type(node) is str:
            return json.dumps(node, ensure_ascii=True)
        if isinstance(node, Decimal):
            return str(node)
        if type(node) is int:
            return str(node)
        _fail()
    return _render(value, ())


def canonical_pairs(argv, *, money=(), json_args=(),
                    money_json_paths=None, bound_args=()):
    money_names = frozenset(_normalise_name(name) for name in money)
    json_names = frozenset(_normalise_name(name) for name in json_args)
    bound_names = tuple(_normalise_name(name) for name in bound_args)
    path_map = {}
    for raw_name, paths in (money_json_paths or {}).items():
        path_map[_normalise_name(raw_name)] = [
            tuple(path) for path in paths]
    for element in argv:
        if type(element) is not str:
            _fail()
    pairs = []
    pos = 0
    while pos < len(argv):
        token = argv[pos]
        if not token.startswith("--"):
            _fail()
        if "=" in token:
            raw_name, value = token[2:].split("=", 1)
        else:
            raw_name = token[2:]
            nxt = pos + 1
            if nxt >= len(argv) or argv[nxt].startswith("--"):
                value = None
            else:
                value = argv[nxt]
                pos = nxt
        name = _normalise_name(raw_name)
        if name == "":
            _fail()
        if name in EXCLUDED_NAMES:
            if name == "user-confirmed" and value is not None:
                _fail()
        else:
            pairs.append([name, value])
        pos += 1
    pairs.sort(key=lambda pair: pair[0])
    for pair in pairs:
        name, value = pair
        if name in json_names:
            if value is None:
                _fail()
            try:
                parsed = json.loads(
                    value,
                    parse_float=Decimal,
                    parse_int=Decimal,
                    parse_constant=_refuse_constant,
                    object_pairs_hook=_refuse_duplicate_keys,
                )
            except ValueError as exc:
                if exc.args == (INPUT_INVALID,):
                    raise
                _fail()
            except Exception:
                _fail()
            wanted = path_map.get(name, [])
            pair[1] = _canonical_json_text(parsed, frozenset(wanted))
        elif name in money_names:
            pair[1] = normalise_money(value)
    for name in bound_names:
        found = False
        for pair in pairs:
            if pair[0] == name and pair[1] is not None:
                found = True
                break
        if not found:
            _fail()
    return pairs


def args_digest(argv, **keywords):
    text = json.dumps(
        canonical_pairs(argv, **keywords),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def envelope_digest(fields):
    if type(fields) is not dict:
        _fail()
    if set(fields.keys()) != _ENVELOPE_KEYS:
        _fail()
    version = fields["v"]
    if type(version) is not int or version != ENVELOPE_VERSION:
        _fail()
    for key in ("binding_digest", "args_digest"):
        value = fields[key]
        if type(value) is not str:
            _fail()
        if _HEX64_RE.fullmatch(value) is None:
            _fail()
    for key in ("issuer_id", "idempotency_key"):
        value = fields[key]
        if type(value) is not str:
            _fail()
        if _ID_RE.fullmatch(value) is None:
            _fail()
    call_id = fields["call_id"]
    if call_id is not None:
        if type(call_id) is not str:
            _fail()
        if _ID_RE.fullmatch(call_id) is None:
            _fail()
    if fields["issued_route"] not in ISSUED_ROUTES:
        _fail()
    reason_code = fields["reason_code"]
    if type(reason_code) is not str:
        _fail()
    if _REASON_CODE_RE.fullmatch(reason_code) is None:
        _fail()
    reason_text = fields["reason_text"]
    if type(reason_text) is not str:
        _fail()
    if not 1 <= len(reason_text) <= REASON_TEXT_MAX:
        _fail()
    issued_at = fields["issued_at"]
    if type(issued_at) is not int:
        _fail()
    if issued_at < 0 or issued_at > _INT_MAX:
        _fail()
    sealed = {
        "args_digest": fields["args_digest"],
        "binding_digest": fields["binding_digest"],
        "call_id": call_id,
        "idempotency_key": fields["idempotency_key"],
        "issued_at": issued_at,
        "issued_route": fields["issued_route"],
        "issuer_id": fields["issuer_id"],
        "reason_code": reason_code,
        "reason_text_sha256": hashlib.sha256(
            reason_text.encode("utf-8")).hexdigest(),
        "v": ENVELOPE_VERSION,
    }
    text = json.dumps(
        sealed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
