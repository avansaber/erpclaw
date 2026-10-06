"""Custom field runtime for ERPClaw.

Allows verticals to extend any core doctype with custom fields
without schema changes. Uses the custom_field and custom_field_value tables.

Schema (from init_db.py):
    custom_field        — field definitions (id, table_name, field_name, field_type, ...)
    custom_field_value  — EAV store (table_name, doc_id, field_name, value)
"""
import json
import re
import uuid
from datetime import time
from decimal import Decimal


# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------

def get_custom_fields(conn, table_name):
    """Return custom field definitions for a table/doctype.

    Args:
        conn: sqlite3 connection (row_factory = sqlite3.Row expected)
        table_name: the doctype/table to query

    Returns:
        list of dicts, one per custom field, ordered by field_name
    """
    rows = conn.execute(
        "SELECT * FROM custom_field WHERE table_name = ? ORDER BY field_name",
        (table_name,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_custom_field(
    conn,
    table_name,
    field_name,
    field_type,
    owner_skill,
    label=None,
    required=False,
    default_value=None,
    field_options=None,
    insert_after=None,
):
    """Register a new custom field definition.

    Args:
        conn: database connection
        table_name: target table/doctype to extend
        field_name: unique field name (snake_case)
        field_type: text, int, float, date, select, link, json, percent,
            duration (whole seconds), rating, or time (local clock)
        owner_skill: skill that owns this custom field
        label: human-readable label (defaults to field_name)
        required: whether field is required (default False)
        default_value: default value as string
        field_options: JSON string with type-specific options
        insert_after: field name to position after in forms

    Returns:
        the generated UUID for the new field definition
    """
    if field_type not in _VALID_FIELD_TYPES:
        raise ValueError("Unsupported custom field type")
    if field_type in _EXTENDED_FIELD_TYPES:
        _rating_limit(field_options) if field_type == "rating" else _no_options(field_options)
        if default_value is not None:
            problem = _extended_value_error(field_type, default_value, field_options)
            if problem:
                raise ValueError(f"Invalid custom field default: {problem}")
    field_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO custom_field
           (id, table_name, field_name, field_type, field_options, label,
            required, default_value, insert_after, owner_skill)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            field_id,
            table_name,
            field_name,
            field_type,
            field_options,
            label or field_name,
            1 if required else 0,
            default_value,
            insert_after,
            owner_skill,
        ),
    )
    return field_id


def remove_custom_field(conn, table_name, field_name, owner_skill):
    """Remove a custom field definition and all its stored values.

    Only the owning skill may remove a field.

    Args:
        conn: database connection
        table_name: the doctype/table the field belongs to
        field_name: field to remove
        owner_skill: must match the original owner_skill

    Returns:
        True if removed, False if not found or wrong owner
    """
    row = conn.execute(
        "SELECT owner_skill FROM custom_field WHERE table_name = ? AND field_name = ?",
        (table_name, field_name),
    ).fetchone()
    if not row or row["owner_skill"] != owner_skill:
        return False
    conn.execute(
        "DELETE FROM custom_field_value WHERE table_name = ? AND field_name = ?",
        (table_name, field_name),
    )
    conn.execute(
        "DELETE FROM custom_field WHERE table_name = ? AND field_name = ?",
        (table_name, field_name),
    )
    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_EXTENDED_FIELD_TYPES = {"percent", "duration", "rating", "time"}
_VALID_FIELD_TYPES = {"text", "int", "float", "date", "select", "link", "json"} | _EXTENDED_FIELD_TYPES


def _no_options(options):
    if options not in (None, ""):
        raise ValueError("This custom field type accepts no options")


def _rating_limit(options):
    if options in (None, ""):
        return 5
    try:
        parsed = json.loads(options)
    except (ValueError, TypeError):
        raise ValueError("Rating options must be a JSON object with integer max") from None
    if (not isinstance(parsed, dict) or set(parsed) != {"max"}
            or type(parsed["max"]) is not int or not 1 <= parsed["max"] <= 100):
        raise ValueError("Rating max must be an integer from 1 to 100")
    return parsed["max"]


def _extended_value_error(field_type, value, options):
    """Validate exact text values without binary floating-point conversion."""
    if value is None or value == "":
        return None
    if type(value) not in (str, int, Decimal):
        return "use text or an exact integer, not a float or boolean"
    text = str(value)
    if field_type == "percent":
        if (not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,6})?", text)
                or Decimal(text) > Decimal("100")):
            return "percent must be from 0 to 100, with at most six decimal places"
    elif field_type == "duration":
        if not re.fullmatch(r"[0-9]{1,18}", text):
            return "duration must be whole nonnegative seconds, at most 18 digits"
    elif field_type == "rating":
        try:
            maximum = _rating_limit(options)
        except ValueError as exc:
            return str(exc)
        if not re.fullmatch(r"[0-9]{1,3}", text) or int(text) > maximum:
            return f"rating must be a whole number from 0 to {maximum}"
    elif field_type == "time":
        if not re.fullmatch(r"[0-9]{2}:[0-9]{2}(?::[0-9]{2})?", text):
            return "time must be a local clock value in HH:MM or HH:MM:SS"
        components = [int(part) for part in text.split(":")]
        if components[0] > 23 or any(part > 59 for part in components[1:]):
            return "time must be a valid local clock value"
        try:
            time.fromisoformat(text)
        except ValueError:
            return "time must be a valid local clock value"
    return None


def validate_custom_field_values(conn, table_name, values):
    """Validate custom field values against their definitions.

    Args:
        conn: database connection
        table_name: the doctype/table being extended
        values: dict of {field_name: value} to validate

    Returns:
        list of error strings (empty = valid)
    """
    fields = {f["field_name"]: f for f in get_custom_fields(conn, table_name)}
    errors = []

    # Check required fields that are missing
    for fname, fdef in fields.items():
        if fdef["required"] and fname not in values:
            errors.append(f"Required custom field '{fname}' is missing")

    # Validate each provided value
    for fname, value in values.items():
        if fname not in fields:
            errors.append(f"Unknown custom field '{fname}' for {table_name}")
            continue

        fdef = fields[fname]
        ftype = fdef["field_type"]

        # None/empty is acceptable for non-required fields
        if value is None or value == "":
            if fdef["required"]:
                errors.append(f"Required custom field '{fname}' cannot be empty")
            continue

        # Type-specific validation
        if ftype in _EXTENDED_FIELD_TYPES:
            problem = _extended_value_error(ftype, value, fdef.get("field_options"))
            if problem:
                errors.append(f"Custom field '{fname}': {problem}")

        elif ftype == "int":
            try:
                int(value)
            except (ValueError, TypeError):
                errors.append(f"Custom field '{fname}' must be an integer")

        elif ftype == "float":
            try:
                float(value)
            except (ValueError, TypeError):
                errors.append(f"Custom field '{fname}' must be a number")

        elif ftype == "date":
            # Expect ISO format YYYY-MM-DD
            if isinstance(value, str):
                import re
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
                    errors.append(
                        f"Custom field '{fname}' must be a date in YYYY-MM-DD format"
                    )

        elif ftype == "select" and fdef.get("field_options"):
            try:
                options = json.loads(fdef["field_options"])
                allowed = options.get("values", [])
                if allowed and value not in allowed:
                    errors.append(
                        f"Custom field '{fname}' must be one of: "
                        f"{', '.join(str(v) for v in allowed)}"
                    )
            except (json.JSONDecodeError, TypeError):
                pass  # malformed options — skip validation

        elif ftype == "json":
            if isinstance(value, str):
                try:
                    json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    errors.append(
                        f"Custom field '{fname}' must be valid JSON"
                    )

        elif ftype == "link" and fdef.get("field_options"):
            # field_options = {"table": "some_table"} — existence check
            try:
                options = json.loads(fdef["field_options"])
                target_table = options.get("table")
                if target_table:
                    if not re.match(r'^[a-z][a-z0-9_]*$', target_table):
                        raise ValueError(f"Invalid table name: {target_table}")
                    # Bare identifier (regex-validated above): portable across
                    # SQLite + Postgres. [bracket] quoting is SQLite-only.
                    exists = conn.execute(
                        f"SELECT 1 FROM {target_table} WHERE id = ?",
                        (value,),
                    ).fetchone()
                    if not exists:
                        errors.append(
                            f"Custom field '{fname}' references non-existent "
                            f"record in {target_table}"
                        )
            except (json.JSONDecodeError, TypeError):
                pass
            except Exception:
                # Table might not exist — skip validation rather than crash
                pass

    return errors


# ---------------------------------------------------------------------------
# Value storage (EAV)
# ---------------------------------------------------------------------------

def store_custom_field_values(conn, table_name, doc_id, values):
    """Store custom field values for a document.

    Uses INSERT ... ON CONFLICT to upsert values.

    Args:
        conn: database connection
        table_name: the doctype/table
        doc_id: the document's primary key (UUID)
        values: dict of {field_name: value}
    """
    for field_name, value in values.items():
        conn.execute(
            """INSERT INTO custom_field_value (table_name, doc_id, field_name, value)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(table_name, doc_id, field_name)
               DO UPDATE SET value = excluded.value""",
            (
                table_name,
                doc_id,
                field_name,
                str(value) if value is not None else None,
            ),
        )


def fetch_custom_field_values(conn, table_name, doc_id):
    """Fetch all custom field values for a document.

    Args:
        conn: database connection
        table_name: the doctype/table
        doc_id: the document's primary key (UUID)

    Returns:
        dict of {field_name: value}
    """
    rows = conn.execute(
        "SELECT field_name, value FROM custom_field_value "
        "WHERE table_name = ? AND doc_id = ?",
        (table_name, doc_id),
    ).fetchall()
    return {r["field_name"]: r["value"] for r in rows}


def delete_custom_field_values(conn, table_name, doc_id):
    """Delete all custom field values for a document.

    Useful when the parent document is deleted.

    Args:
        conn: database connection
        table_name: the doctype/table
        doc_id: the document's primary key (UUID)
    """
    conn.execute(
        "DELETE FROM custom_field_value WHERE table_name = ? AND doc_id = ?",
        (table_name, doc_id),
    )


def apply_defaults(conn, table_name, values):
    """Fill in default values for any custom fields not already provided.

    Args:
        conn: database connection
        table_name: the doctype/table
        values: dict of {field_name: value} — modified in-place

    Returns:
        the (possibly augmented) values dict
    """
    for fdef in get_custom_fields(conn, table_name):
        fname = fdef["field_name"]
        if fname not in values and fdef["default_value"] is not None:
            values[fname] = fdef["default_value"]
    return values


# ---------------------------------------------------------------------------
# Wrapper integration helpers (M1 — used by add-*/get-* actions in
# selling/buying/inventory so callers never touch the EAV tables directly)
# ---------------------------------------------------------------------------

def store_from_arg(conn, table_name, doc_id, custom_fields_arg):
    """Parse a `--custom-fields` JSON-object arg, apply defaults, validate, store.

    The single point a write-side wrapper calls after inserting its primary row
    (before commit, so a validation failure can be rolled back cleanly).

    Args:
        conn: database connection
        table_name: the doctype/table the row belongs to
        doc_id: the just-inserted row's id
        custom_fields_arg: the raw `--custom-fields` value (JSON object string),
            or None / "" when the caller passed no custom fields

    Returns:
        list of error strings (empty = stored, or nothing to do). On a non-empty
        return the caller should roll back and surface the errors; nothing was
        written to custom_field_value.
    """
    if not custom_fields_arg:
        return []
    try:
        values = json.loads(custom_fields_arg)
    except (json.JSONDecodeError, TypeError):
        return ["--custom-fields must be a valid JSON object, e.g. '{\"priority\": \"Gold\"}'"]
    if not isinstance(values, dict):
        return ["--custom-fields must be a JSON object mapping field names to values"]
    apply_defaults(conn, table_name, values)
    errors = validate_custom_field_values(conn, table_name, values)
    if errors:
        return errors
    store_custom_field_values(conn, table_name, doc_id, values)
    return []


def merge_into_response(conn, table_name, doc_id, response):
    """Attach a row's stored custom field values to a `get-*` response dict.

    Adds a `custom_fields` key only when the row has stored values, so responses
    for rows without UDFs stay unchanged. Returns the same dict for chaining.
    """
    values = fetch_custom_field_values(conn, table_name, doc_id)
    if values:
        response["custom_fields"] = values
    return response
