"""Shared test helpers for erpclaw-setup tests.

Provides _call_action() to invoke action functions directly and capture
their JSON output (which they print to stdout before calling sys.exit).
"""
import argparse
import io
import json
import os
import sys

# Add scripts to path for importing db_query
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Add shared lib
LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

# Monorepo root (contains init_db.py)
_MONOREPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../.."))
_SERVER_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "erpclaw-setup", "scripts")
if os.path.exists(os.path.join(_MONOREPO_ROOT, "init_db.py")):
    PROJECT_ROOT = _MONOREPO_ROOT
elif os.path.exists(os.path.join(_SERVER_ROOT, "init_db.py")):
    PROJECT_ROOT = _SERVER_ROOT
else:
    PROJECT_ROOT = _MONOREPO_ROOT


# ---------------------------------------------------------------------------
# Default argument namespace for _call_action
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    "name": None,
    "abbr": None,
    "currency": None,
    "country": None,
    "company_id": None,
    "tax_id": None,
    "fiscal_year_start_month": None,
    "code": None,
    "symbol": None,
    "decimal_places": None,
    "enabled": False,
    "enabled_only": False,
    "from_currency": None,
    "to_currency": None,
    "rate": None,
    "effective_date": None,
    "source": None,
    "due_days": None,
    "discount_percentage": None,
    "discount_days": None,
    "description": None,
    "must_be_whole_number": False,
    "from_uom": None,
    "to_uom": None,
    "conversion_factor": None,
    "item_id": None,
    "entity_type": None,
    "entity_id": None,
    "audit_action": None,
    "from_date": None,
    "to_date": None,
    "limit": None,
    "offset": None,
    "module": None,
    "date_format": None,
    "number_format": None,
    "default_tax_template_id": None,
    "backup_path": None,
    "encrypt": False,
    "passphrase": None,
    "db_path": None,
    "default_receivable_account_id": None,
    "default_payable_account_id": None,
    "default_income_account_id": None,
    "default_expense_account_id": None,
    "default_cost_center_id": None,
    "default_warehouse_id": None,
    "default_bank_account_id": None,
    "default_cash_account_id": None,
    "round_off_account_id": None,
    "exchange_gain_loss_account_id": None,
    "perpetual_inventory": None,
    "enable_negative_stock": None,
    "accounts_frozen_till_date": None,
    "role_allowed_for_frozen_entries": None,
    "default_currency": None,
    "user_id": None,
    "email": None,
    "full_name": None,
    "user_status": None,
    "role_name": None,
    "password": None,
    "telegram_user_id": None,
    "skill": None,
    "check_action": None,
    "force": False,
    "answer": None,
    "reset": False,
}


def _call_action(action_fn, conn, **kwargs):
    """Call an action function and return the parsed JSON output.

    Intercepts sys.stdout and catches SystemExit (raised by _ok / _err).
    Returns a dict with the parsed JSON response.
    """
    merged = {**_DEFAULT_ARGS, **kwargs}
    args = argparse.Namespace(**merged)

    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        action_fn(conn, args)
    except SystemExit:
        pass
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    return json.loads(output)


def _run_init_db(db_path: str):
    """Execute init_db.py to create all tables."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "init_db", os.path.join(PROJECT_ROOT, "init_db.py")
    )
    init_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_db)
    init_db.init_db(db_path)
