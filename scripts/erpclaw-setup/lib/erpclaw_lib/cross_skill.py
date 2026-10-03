"""Cross-skill integration API for ERPClaw verticals.

Provides high-level functions that vertical skills (PropertyClaw, BuildClaw,
LegalClaw, etc.) call to interact with core ERPClaw skills — without
directly importing or writing to tables they don't own.

Each function:
1. Validates inputs
2. Resolves the target skill's db_query.py via dependencies.resolve_skill_script()
3. Calls it via subprocess with the correct --action and args
4. Parses the JSON response
5. Returns a structured result or raises CrossSkillError

Usage:
    from erpclaw_lib.cross_skill import create_invoice, create_payment, call_skill_action

    # High-level: create a sales invoice from a vertical
    result = create_invoice(
        company_id="...",
        customer_id="...",
        items=[{"description": "Monthly rent", "qty": "1", "rate": "2500.00"}],
    )
    invoice_id = result["sales_invoice_id"]

    # Low-level: call any action on any skill
    result = call_skill_action("erpclaw", "list-customers",
                               {"--company-id": company_id})
"""
import json
import os
import subprocess
import sys
from typing import Optional

from erpclaw_lib import actor
from erpclaw_lib.dependencies import resolve_skill_script, check_subprocess_target


def child_interpreter() -> str:
    """Interpreter for child skill actions.

    Returns the interpreter running this process so a child action
    inherits the caller's environment. Falls back to "python3" when
    the running interpreter is unknown.
    """
    return sys.executable or "python3"


class CrossSkillError(Exception):
    """Raised when a cross-skill call fails."""

    def __init__(self, message, skill=None, action=None, raw_output=None):
        super().__init__(message)
        self.skill = skill
        self.action = action
        self.raw_output = raw_output


def call_skill_action(
    skill_name: str,
    action: str,
    args: Optional[dict] = None,
    db_path: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Call an action on any installed skill via subprocess.

    This is the low-level building block. Higher-level functions like
    create_invoice() and create_payment() use this internally.

    Args:
        skill_name: e.g. 'erpclaw'
        action: e.g. 'create-sales-invoice'
        args: Dict of CLI arguments. Keys should include '--' prefix.
              e.g. {"--customer-id": "abc", "--items": '[...]'}
        db_path: Optional non-default DB path to pass through.
        timeout: Subprocess timeout in seconds (default 30).

    Returns:
        Parsed JSON dict from the skill's stdout.

    Raises:
        CrossSkillError: If skill not found, subprocess fails, or response invalid.
    """
    script_path = resolve_skill_script(skill_name)
    if not script_path:
        raise CrossSkillError(
            f"{skill_name} is not installed. Install it first.",
            skill=skill_name,
            action=action,
        )

    cmd = [child_interpreter(), script_path, "--action", action]

    if args:
        for key, value in args.items():
            cmd.append(key)
            if value is not None:
                cmd.append(str(value))

    if db_path:
        cmd.extend(["--db-path", db_path])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=actor.child_env(os.environ, "%s:%s" % (skill_name, action)),
        )
    except subprocess.TimeoutExpired:
        raise CrossSkillError(
            f"{skill_name} {action} timed out after {timeout}s",
            skill=skill_name,
            action=action,
        )

    if result.returncode != 0:
        raw = result.stdout.strip() or result.stderr.strip()
        # Try to extract structured error
        try:
            err_data = json.loads(raw)
            msg = err_data.get("message", err_data.get("error", raw[:500]))
            # Surface module install suggestion if available
            suggested = err_data.get("suggested_module")
            if suggested:
                msg = (f"Action '{action}' requires module '{suggested}' which is not installed. "
                       f"Install it with: install-module --module-name {suggested}")
        except (json.JSONDecodeError, TypeError):
            msg = raw[:500] if raw else "Unknown error (no output)"
        raise CrossSkillError(
            f"{skill_name} {action} failed: {msg}",
            skill=skill_name,
            action=action,
            raw_output=raw,
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise CrossSkillError(
            f"Invalid JSON from {skill_name} {action}: {result.stdout[:200]}",
            skill=skill_name,
            action=action,
            raw_output=result.stdout,
        )

    if data.get("status") == "error":
        raise CrossSkillError(
            data.get("message", "Unknown error"),
            skill=skill_name,
            action=action,
            raw_output=result.stdout,
        )

    return data


# ---------------------------------------------------------------------------
# High-level: Invoice creation
# ---------------------------------------------------------------------------

def create_invoice(
    customer_id: str,
    items: list[dict],
    company_id: Optional[str] = None,
    posting_date: Optional[str] = None,
    due_date: Optional[str] = None,
    project_id: Optional[str] = None,
    remarks: Optional[str] = None,
    db_path: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Create a Sales Invoice via erpclaw.

    This is the standard way for verticals to generate invoices.
    Creates a draft invoice that can be submitted separately.

    Args:
        customer_id: The customer to invoice.
        items: List of item dicts, each with at minimum:
               {"description": str, "qty": str, "rate": str}
               Optional: {"item_id": str, "uom": str, "warehouse_id": str}
        company_id: Company ID (passed if needed).
        posting_date: Invoice date (YYYY-MM-DD). Defaults to today in skill.
        due_date: Payment due date (YYYY-MM-DD).
        project_id: Link invoice to a project.
        remarks: Free-text remarks on the invoice.
        db_path: Non-default DB path.
        timeout: Subprocess timeout.

    Returns:
        The selling module's flat response dict, unchanged:
        {"status": "ok", "sales_invoice_id": "...", "grand_total": ...}

    Raises:
        CrossSkillError: If selling skill not installed or invoice creation fails.
            Also raised when project_id or remarks is passed: neither is
            carried by create-sales-invoice, so passing one is refused loudly
            instead of being dropped silently.
    """
    if project_id is not None:
        raise CrossSkillError(
            "create_invoice: project_id is not carried by create-sales-invoice; "
            "remove it or put the text on the vertical's own record",
            skill="erpclaw",
            action="create-sales-invoice",
        )
    if remarks is not None:
        raise CrossSkillError(
            "create_invoice: remarks is not carried by create-sales-invoice; "
            "remove it or put the text on the vertical's own record",
            skill="erpclaw",
            action="create-sales-invoice",
        )
    resolved_items = []
    service_item_id = None
    for item in items:
        if item.get("item_id"):
            resolved_items.append(dict(item))
            continue
        if service_item_id is None:
            service_item_id = ensure_service_item(
                company_id, db_path=db_path, timeout=timeout,
            )
        line = dict(item)
        line["item_id"] = service_item_id
        resolved_items.append(line)
    args = {
        "--customer-id": customer_id,
        "--items": json.dumps(resolved_items),
    }
    if company_id:
        args["--company-id"] = company_id
    if posting_date:
        args["--posting-date"] = posting_date
    if due_date:
        args["--due-date"] = due_date

    return call_skill_action(
        "erpclaw", "create-sales-invoice",
        args=args, db_path=db_path, timeout=timeout,
    )


_SERVICE_ITEM_CACHE = {}


def ensure_service_item(
    company_id,
    db_path=None,
    timeout=30,
    *,
    item_code=None,
    item_name="Vertical Service",
) -> str:
    """Return the id of the company-scoped generic service item.

    The selling module refuses invoice lines without an item_id, but verticals
    carry no item reference — so description-only lines bill against one
    generic service item per company, created through the inventory add-item
    action and never by writing the item table directly (this library opens no
    database connection; everything goes through actions).

    On an add-item refusal (most likely a duplicate code from a concurrent or
    earlier invoice), the item is looked up by code through the inventory
    list-items action (--search filter) and the existing id is reused. Results
    are cached per (company_id, item_code, db_path) so one invoice with many
    lines makes one lookup.

    Raises:
        CrossSkillError: If neither path yields an id.
    """
    code = item_code or (f"SVC-{company_id}" if company_id else "SVC-DEFAULT")
    key = (company_id, code, db_path)
    if key in _SERVICE_ITEM_CACHE:
        return _SERVICE_ITEM_CACHE[key]
    try:
        created = call_skill_action(
            "erpclaw", "add-item",
            args={"--item-code": code, "--item-name": item_name,
                  "--item-type": "service"},
            db_path=db_path, timeout=timeout,
        )
    except CrossSkillError:
        created = None
    if created is not None:
        new_id = created.get("item_id") or created.get("id")
        if new_id:
            _SERVICE_ITEM_CACHE[key] = new_id
            return new_id
    found = call_skill_action(
        "erpclaw", "list-items",
        args={"--search": code},
        db_path=db_path, timeout=timeout,
    )
    for row in found.get("items", []) or []:
        if row.get("item_code") == code and row.get("id"):
            _SERVICE_ITEM_CACHE[key] = row["id"]
            return row["id"]
    raise CrossSkillError(
        f"ensure_service_item: no service item for code {code}",
        skill="erpclaw",
        action="list-items",
    )


def submit_invoice(
    invoice_id: str,
    db_path: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Submit (finalize) a Sales Invoice via erpclaw.

    This validates the invoice, posts GL entries, and transitions
    the invoice from Draft to Submitted.

    Args:
        invoice_id: The sales_invoice.id to submit.
        db_path: Non-default DB path.
        timeout: Subprocess timeout.

    Returns:
        Response dict from erpclaw.

    Raises:
        CrossSkillError: On failure.
    """
    # submit-sales-invoice is a gated high-impact action: the foundation
    # router requires a per-invocation --user-confirmed and strips it before
    # forwarding, so the selling parser never sees it.
    return call_skill_action(
        "erpclaw", "submit-sales-invoice",
        args={"--sales-invoice-id": invoice_id, "--user-confirmed": None},
        db_path=db_path, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# High-level: Purchase Invoice creation
# ---------------------------------------------------------------------------

def create_purchase_invoice(
    supplier_id: str,
    items: list[dict],
    company_id: Optional[str] = None,
    posting_date: Optional[str] = None,
    due_date: Optional[str] = None,
    project_id: Optional[str] = None,
    remarks: Optional[str] = None,
    db_path: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Create a Purchase Invoice via erpclaw.

    This is the standard way for verticals to generate purchase invoices.
    Creates a draft invoice that can be submitted separately.

    Args:
        supplier_id: The supplier to invoice.
        items: List of item dicts, each with at minimum:
               {"description": str, "qty": str, "rate": str}
               Optional: {"item_id": str, "uom": str}
        company_id: Company ID (passed if needed).
        posting_date: Invoice date (YYYY-MM-DD). Defaults to today in skill.
        due_date: Payment due date (YYYY-MM-DD).
        project_id: Link invoice to a project.
        remarks: Free-text remarks on the invoice.
        db_path: Non-default DB path.
        timeout: Subprocess timeout.

    Returns:
        The buying module's flat response dict, unchanged:
        {"status": "ok", "purchase_invoice_id": "...", "grand_total": ...}

    Raises:
        CrossSkillError: If buying skill not installed or invoice creation fails.
            Also raised when project_id or remarks is passed: neither is
            carried by create-purchase-invoice, so passing one is refused loudly
            instead of being dropped silently.
    """
    if project_id is not None:
        raise CrossSkillError(
            "create_purchase_invoice: project_id is not carried by create-purchase-invoice; "
            "remove it or put the text on the vertical's own record",
            skill="erpclaw",
            action="create-purchase-invoice",
        )
    if remarks is not None:
        raise CrossSkillError(
            "create_purchase_invoice: remarks is not carried by create-purchase-invoice; "
            "remove it or put the text on the vertical's own record",
            skill="erpclaw",
            action="create-purchase-invoice",
        )
    resolved_items = []
    service_item_id = None
    for item in items:
        if item.get("item_id"):
            resolved_items.append(dict(item))
            continue
        if service_item_id is None:
            service_item_id = ensure_service_item(
                company_id, db_path=db_path, timeout=timeout,
            )
        line = dict(item)
        line["item_id"] = service_item_id
        resolved_items.append(line)
    args = {
        "--supplier-id": supplier_id,
        "--items": json.dumps(resolved_items),
    }
    if company_id:
        args["--company-id"] = company_id
    if posting_date:
        args["--posting-date"] = posting_date
    if due_date:
        args["--due-date"] = due_date

    return call_skill_action(
        "erpclaw", "create-purchase-invoice",
        args=args, db_path=db_path, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# High-level: Payment creation
# ---------------------------------------------------------------------------

def create_payment(
    payment_type: str,
    party_type: str,
    party_id: str,
    paid_amount: str,
    company_id: Optional[str] = None,
    posting_date: Optional[str] = None,
    paid_from: Optional[str] = None,
    paid_to: Optional[str] = None,
    reference_type: Optional[str] = None,
    reference_id: Optional[str] = None,
    payment_method: Optional[str] = None,
    remarks: Optional[str] = None,
    db_path: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Create a Payment Entry via erpclaw-payments.

    Args:
        payment_type: 'receive' (from customer) or 'pay' (to supplier/employee).
        party_type: 'customer', 'supplier', or 'employee'.
        party_id: The party's ID.
        paid_amount: Amount as string (Decimal-safe).
        company_id: Company ID.
        posting_date: Payment date.
        paid_from: Source account ID (bank/cash).
        paid_to: Target account ID.
        reference_type: e.g. 'sales_invoice', 'purchase_invoice'.
        reference_id: The referenced document ID.
        payment_method: e.g. 'bank_transfer', 'cash', 'check'.
        remarks: Free-text remarks.
        db_path: Non-default DB path.
        timeout: Subprocess timeout.

    Returns:
        Response dict from erpclaw-payments.

    Raises:
        CrossSkillError: On failure.
    """
    args = {
        "--payment-type": payment_type,
        "--party-type": party_type,
        "--party-id": party_id,
        "--paid-amount": paid_amount,
    }
    if company_id:
        args["--company-id"] = company_id
    if posting_date:
        args["--posting-date"] = posting_date
    if paid_from:
        args["--paid-from"] = paid_from
    if paid_to:
        args["--paid-to"] = paid_to
    if reference_type:
        args["--reference-type"] = reference_type
    if reference_id:
        args["--reference-id"] = reference_id
    if payment_method:
        args["--payment-method"] = payment_method
    if remarks:
        args["--remarks"] = remarks

    return call_skill_action(
        "erpclaw-payments", "add-payment-entry",
        args=args, db_path=db_path, timeout=timeout,
    )


def submit_payment(
    payment_id: str,
    db_path: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Submit a Payment Entry via erpclaw-payments.

    Args:
        payment_id: The payment_entry.id to submit.
        db_path: Non-default DB path.
        timeout: Subprocess timeout.

    Returns:
        Response dict from erpclaw-payments.

    Raises:
        CrossSkillError: On failure.
    """
    return call_skill_action(
        "erpclaw-payments", "submit-payment-entry",
        args={"--payment-id": payment_id},
        db_path=db_path, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# High-level: Customer / Supplier creation
# ---------------------------------------------------------------------------

def create_customer(
    customer_name: str,
    company_id: Optional[str] = None,
    customer_type: str = "company",
    email: Optional[str] = None,
    phone: Optional[str] = None,
    db_path: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Create a Customer via erpclaw.

    This is the correct way for verticals to create customers.
    DO NOT INSERT directly into the customer table.

    Args:
        customer_name: Customer name.
        company_id: Company ID.
        customer_type: 'company' or 'individual'.
        email: Customer email.
        phone: Customer phone.
        db_path: Non-default DB path.
        timeout: Subprocess timeout.

    Returns:
        Response dict with customer ID.

    Raises:
        CrossSkillError: On failure.
    """
    args = {"--name": customer_name}
    if company_id:
        args["--company-id"] = company_id
    if customer_type:
        args["--customer-type"] = customer_type
    if email:
        args["--email"] = email
    if phone:
        args["--phone"] = phone

    return call_skill_action(
        "erpclaw", "add-customer",
        args=args, db_path=db_path, timeout=timeout,
    )


def create_supplier(
    supplier_name: str,
    company_id: Optional[str] = None,
    supplier_type: str = "company",
    email: Optional[str] = None,
    phone: Optional[str] = None,
    db_path: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Create a Supplier via erpclaw.

    Args:
        supplier_name: Supplier name.
        company_id: Company ID.
        supplier_type: 'company' or 'individual'.
        email: Supplier email.
        phone: Supplier phone.
        db_path: Non-default DB path.
        timeout: Subprocess timeout.

    Returns:
        Response dict with supplier ID.

    Raises:
        CrossSkillError: On failure.
    """
    args = {"--name": supplier_name}
    if company_id:
        args["--company-id"] = company_id
    if supplier_type:
        args["--supplier-type"] = supplier_type
    if email:
        args["--email"] = email
    if phone:
        args["--phone"] = phone

    return call_skill_action(
        "erpclaw", "add-supplier",
        args=args, db_path=db_path, timeout=timeout,
    )
