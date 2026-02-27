"""Naming series management for ERPClaw.

Generates sequential document numbers like INV-2026-00001.
Pattern: {PREFIX}{YEAR}-{SEQUENCE}

Each entity type has a unique prefix. The sequence counter is stored
in the `naming_series` table and incremented atomically per call.
"""
import uuid
import sqlite3
from datetime import datetime, timezone


# Maps entity type to its naming prefix
ENTITY_PREFIXES = {
    # erpclaw-setup
    "company": "COMP-",
    # erpclaw-gl
    "account": "ACC-",
    "fiscal_year": "FY-",
    "cost_center": "CC-",
    "period_closing_voucher": "PCV-",
    # erpclaw-journals
    "journal_entry": "JE-",
    "recurring_journal_template": "RJT-",
    # erpclaw-payments
    "payment_entry": "PAY-",
    # erpclaw-tax
    "tax_template": "TAX-",
    "tax_withholding_entry": "TWE-",
    # erpclaw-selling
    "quotation": "QTN-",
    "sales_order": "SO-",
    "delivery_note": "DN-",
    "sales_invoice": "INV-",
    "credit_note": "CN-",
    "recurring_invoice_template": "RIT-",
    # erpclaw-buying
    "material_request": "MR-",
    "request_for_quotation": "RFQ-",
    "purchase_order": "PO-",
    "purchase_receipt": "PR-",
    "purchase_invoice": "PINV-",
    "debit_note": "DBN-",
    # erpclaw-inventory
    "stock_entry": "STE-",
    "stock_reconciliation": "STR-",
    "stock_revaluation": "SREVAL-",
    # erpclaw-manufacturing
    "work_order": "WO-",
    "bom": "BOM-",
    "job_card": "JC-",
    "production_plan": "PP-",
    "subcontracting_order": "SCO-",
    # erpclaw-hr
    "employee": "EMP-",
    "salary_slip": "SS-",
    "payroll_run": "PRUN-",
    "expense_claim": "EC-",
    "leave_application": "LA-",
    # erpclaw-assets
    "asset": "AST-",
    # erpclaw-projects
    "project": "PRJ-",
    "task": "TSK-",
    "timesheet": "TS-",
    # erpclaw-quality
    "quality_inspection": "QC-",
    "non_conformance": "NC-",
    # erpclaw-crm
    "lead": "LEAD-",
    "opportunity": "OPP-",
    # erpclaw-support
    "issue": "ISS-",
    "warranty_claim": "WC-",
    "maintenance_schedule": "MS-",
    "maintenance_visit": "MV-",
    # erpclaw-billing
    "meter": "MTR-",
}


def get_next_name(conn: sqlite3.Connection, entity_type: str,
                  year: int = None, company_id: str = None) -> str:
    """Generate the next sequential name for an entity type.

    Uses the naming_series table to track and increment counters.
    The year is embedded in the stored prefix for annual resets.

    Thread-safe: uses SQLite's atomic INSERT OR UPDATE via UNIQUE
    constraint on (entity_type, prefix, company_id).

    Args:
        conn: SQLite connection (caller manages transaction).
        entity_type: Key from ENTITY_PREFIXES (e.g., "sales_invoice").
        year: Year for the series. Defaults to current year.
        company_id: Company ID. If conn has a company_id attribute
                    (e.g., TestDB), that is used as fallback.

    Returns:
        Formatted name like "INV-2026-00001".

    Raises:
        ValueError: If entity_type is not in ENTITY_PREFIXES.
    """
    if entity_type not in ENTITY_PREFIXES:
        raise ValueError(
            f"Unknown entity type '{entity_type}'. "
            f"Valid types: {sorted(ENTITY_PREFIXES.keys())}"
        )

    base_prefix = ENTITY_PREFIXES[entity_type]
    if year is None:
        year = datetime.now(timezone.utc).year

    # Resolve company_id from parameter or conn attribute
    if company_id is None:
        company_id = getattr(conn, "company_id", None)
    if not company_id:
        raise ValueError("company_id is required for naming series")

    # Store year-scoped prefix so annual reset is automatic
    year_prefix = f"{base_prefix}{year}-"
    entry_id = str(uuid.uuid4())

    # Atomic increment: INSERT if new, UPDATE if exists
    conn.execute(
        """
        INSERT INTO naming_series (id, entity_type, prefix, current_value, company_id)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(entity_type, prefix, company_id)
        DO UPDATE SET current_value = current_value + 1
        """,
        (entry_id, entity_type, year_prefix, company_id),
    )

    row = conn.execute(
        "SELECT current_value FROM naming_series WHERE entity_type = ? AND prefix = ? AND company_id = ?",
        (entity_type, year_prefix, company_id),
    ).fetchone()

    sequence = row[0]
    return f"{base_prefix}{year}-{sequence:05d}"


def parse_name(name: str) -> dict:
    """Parse a naming series string back to its components.

    Args:
        name: e.g., "INV-2026-00001"

    Returns:
        {"prefix": "INV-", "year": 2026, "sequence": 1}

    Raises:
        ValueError: If the name doesn't match the expected pattern.
    """
    parts = name.rsplit("-", 2)
    if len(parts) < 3:
        raise ValueError(f"Cannot parse name '{name}': expected PREFIX-YEAR-SEQUENCE")

    # Reconstruct prefix (everything before the last two segments)
    # Handle multi-part prefixes like "PINV-"
    sequence_str = parts[-1]
    year_str = parts[-2]
    prefix = name[: len(name) - len(year_str) - len(sequence_str) - 2] + "-"

    try:
        year = int(year_str)
        sequence = int(sequence_str)
    except ValueError:
        raise ValueError(f"Cannot parse name '{name}': year and sequence must be integers")

    return {"prefix": prefix, "year": year, "sequence": sequence}
