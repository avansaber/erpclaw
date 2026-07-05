"""Action-handler validators that emit an err() response and exit on failure.

These wrap the ValueError-raising primitives in ``validation.py`` (e.g.
``require_entity``) with the ``err()``-exit contract used by integration
action handlers: on a validation failure they print the standard
``{"status": "error", ...}`` JSON to stdout and ``sys.exit(1)`` rather than
raising.

Hoisted out of the Stripe and Shopify integration helper modules (M31 H6),
where three byte-identical copies (``validate_company`` / ``validate_account_exists``
/ ``validate_enum``) had drifted into existence. The error messages are
reproduced verbatim because the NL agent and the addon test suites depend on
them.
"""
from __future__ import annotations

from .response import err
from .validation import require_entity


def validate_company(conn, company_id):
    """Validate that a company exists. Calls err() and exits if not found."""
    if not company_id:
        err("--company-id is required")
    try:
        require_entity(conn, "company", company_id, "Company")
    except ValueError:
        err(f"Company {company_id} not found")


def validate_account_exists(conn, account_id, label="Account"):
    """Validate that a GL account exists. Calls err() and exits if not found."""
    if not account_id:
        return  # Optional field
    try:
        require_entity(conn, "account", account_id, label)
    except ValueError:
        err(f"{label} {account_id} not found in chart of accounts")


def validate_enum(value, valid_values, field_name):
    """Validate that a value is in the allowed set. Calls err() if invalid."""
    if value and value not in valid_values:
        err(f"Invalid {field_name}: {value}. Must be one of: {', '.join(valid_values)}")
