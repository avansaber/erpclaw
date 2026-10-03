"""Accounting dimensions shared by business-document drafts.

A business document carries a small set of accounting dimensions (for
example ``{"department": "Engineering"}``), checked when the draft is
written and later copied onto every ledger row the document posts. This
module holds the three pieces every later task calls:

- :func:`parse_dimension_input` turns the ``--dimensions`` / ``--dimension-key``
  / ``--dimension-value`` CLI spellings into one ``dict[str, str]``.
- :func:`validate_document_dimensions` checks that dict against the active
  ``dimension_registry`` rows on the caller's own connection.
- :func:`dimensions_json_text` serialises the dict exactly the way the GL
  writer does, so a document column and its ledger rows hold byte-identical
  text.

Ledger posting keeps its own (looser) checks; the stricter rules here are
for drafts only.
"""
import json
import re

from erpclaw_lib.db import db_error_types, get_dialect
from erpclaw_lib.query import Field, P, Q, Table

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _checked_key(raw_key):
    if not isinstance(raw_key, str) or raw_key.strip() == "":
        raise ValueError(
            "Dimension '%s' must be a non-empty string" % (raw_key,))
    return raw_key.strip()


def _checked_value(raw_key, raw_value):
    if not isinstance(raw_value, str) or raw_value.strip() == "":
        raise ValueError(
            "Dimension '%s' must be a non-empty string" % (raw_key,))
    return raw_value.strip()


def _merge(merged, key, value):
    if key in merged and merged[key] != value:
        raise ValueError(
            "Dimension '%s' given twice with different values" % (key,))
    merged[key] = value


def parse_dimension_input(dimensions_json, keys, values):
    """Combine the ``--dimensions`` JSON object with ``--dimension-key`` / ``--dimension-value`` pairs.

    Returns ``None`` when the caller asked for nothing (all three inputs
    empty), otherwise a ``dict[str, str]`` with keys and values stripped.
    An explicit ``'{}'`` returns ``{}``: the caller clears the set. The same
    key given twice with the same value is accepted once.
    """
    if keys is None:
        keys = []
    if values is None:
        values = []
    has_json = dimensions_json is not None and dimensions_json != ""
    if not has_json and not keys and not values:
        return None
    merged = {}
    if has_json:
        if not isinstance(dimensions_json, str):
            raise ValueError("--dimensions is not valid JSON")
        try:
            parsed = json.loads(dimensions_json)
        except (ValueError, TypeError):
            raise ValueError("--dimensions is not valid JSON")
        if not isinstance(parsed, dict):
            raise ValueError("--dimensions must be a JSON object")
        for raw_key, raw_value in parsed.items():
            _merge(merged, _checked_key(raw_key),
                   _checked_value(raw_key, raw_value))
    if len(keys) != len(values):
        raise ValueError(
            "--dimension-key and --dimension-value must be given in pairs")
    for raw_key, raw_value in zip(keys, values):
        _merge(merged, _checked_key(raw_key),
               _checked_value(raw_key, raw_value))
    return merged


def _optional_list(raw):
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


def _check_reference(conn, key, value, referenced_table):
    if (not referenced_table
            or not isinstance(referenced_table, str)
            or not _IDENTIFIER_RE.match(referenced_table)):
        raise ValueError(
            "Dimension '%s' has an invalid referenced table" % (key,))
    target = Table(referenced_table)
    lookup = (Q.from_(target).select(Field("id"))
              .where(Field("id") == P()).get_sql())
    conn.execute("SAVEPOINT erpclaw_dimension_probe")
    try:
        found = conn.execute(lookup, (value,)).fetchone()
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT erpclaw_dimension_probe")
        conn.execute("RELEASE SAVEPOINT erpclaw_dimension_probe")
        missing, _base = db_error_types()
        if (isinstance(exc, missing)
                and (get_dialect() != "sqlite"
                     or "no such table" in str(exc).lower())):
            raise ValueError(
                "Dimension '%s' has an invalid referenced table" % (key,))
        raise
    conn.execute("RELEASE SAVEPOINT erpclaw_dimension_probe")
    if found is None:
        raise ValueError(
            "Dimension '%s' references %s id '%s' which does not exist"
            % (key, referenced_table, value))


def validate_document_dimensions(conn, dims, account_ids=()):
    """Check ``dims`` against the active registry rows; refuse the first failure.

    Keys are checked in sorted order, then required-dimension coverage for
    each id in ``account_ids`` in the order given. ``dims`` of ``None`` is
    treated as ``{}``; empty ``dims`` with no ``account_ids`` returns at
    once without touching the connection.
    """
    if dims is None:
        dims = {}
    if account_ids is None:
        account_ids = []
    else:
        account_ids = list(account_ids)
    if not dims and not account_ids:
        return
    reg_table = Table("dimension_registry")
    reg_query = (Q.from_(reg_table)
                 .select(Field("key"), Field("data_type"),
                         Field("referenced_table"),
                         Field("allowed_values_json"),
                         Field("is_required_on_account_types_json"))
                 .where(Field("is_active") == 1))
    registry = {}
    for row in conn.execute(reg_query.get_sql()).fetchall():
        registry[row["key"]] = {
            "data_type": row["data_type"],
            "referenced_table": row["referenced_table"],
            "allowed_values_json": row["allowed_values_json"],
            "required_json": row["is_required_on_account_types_json"],
        }
    for key in sorted(dims):
        raw = dims[key]
        entry = registry.get(key)
        if entry is None:
            raise ValueError(
                "Unknown or inactive dimension '%s'; run list-dimensions"
                % (key,))
        if raw is None or str(raw).strip() == "":
            continue
        if entry["data_type"] == "enum":
            allowed = _optional_list(entry["allowed_values_json"])
            if allowed is not None and raw not in allowed:
                raise ValueError(
                    "Dimension '%s' value '%s' is not one of its allowed values"
                    % (key, raw))
        elif entry["data_type"] == "uuid_fk":
            _check_reference(conn, key, raw, entry["referenced_table"])
    acct_table = Table("account")
    for account_id in account_ids:
        acct_query = (Q.from_(acct_table)
                      .select(Field("id"), Field("name"),
                              Field("account_type"))
                      .where(Field("id") == P()).get_sql())
        row = conn.execute(acct_query, (account_id,)).fetchone()
        if row is None:
            raise ValueError("Account '%s' not found" % (account_id,))
        acct_type = row["account_type"] or ""
        for key in sorted(registry):
            required = _optional_list(registry[key]["required_json"])
            if required is not None and acct_type in required:
                raw = dims.get(key)
                if raw is None or str(raw).strip() == "":
                    raise ValueError(
                        "Dimension '%s' is required for account '%s' "
                        "(account_type '%s')" % (key, row["name"], acct_type))


def dimensions_json_text(dims):
    """Serialise ``dims`` the way the GL writer does (sorted keys)."""
    return json.dumps(dims or {}, sort_keys=True)
