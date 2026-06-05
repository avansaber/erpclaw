"""Canonical voucher-type normalization (neutral shared lib).

The OpenClaw gateway can hand an action a Title-Case doctype LABEL
("Sales Invoice") where the code expects the canonical snake_case key
("sales_invoice"). FINDING-005 proved this for payment allocations: a stored
label voucher_type silently no-op'd because every comparison (PLE netting,
clearing, doctype filters) is against the snake_case form selling/buying post.

FINDING-006 generalizes the fix: any gateway-facing action that STORES or
FILTERS a doctype voucher_type / reference_type / against_voucher_type should
canonicalize the value right after reading it from args. This module is the ONE
home for that rule so every module can import it without depending on the
payments-specific ``payment_clearing.py``.

Design rules:
- Case-insensitive; spaces and hyphens are treated as underscores.
- Known doctype labels map to their canonical snake_case key.
- Values that are already canonical pass through unchanged.
- Genuinely-unknown values (after normalization) are returned VERBATIM, never
  mangled — advance / on-account / custom voucher types stay as-is. This makes
  the normalizer safe to apply to any doctype voucher_type field even when the
  caller already passed snake_case.
"""

# Canonical snake_case voucher types that appear as ``voucher_type`` /
# ``reference_type`` / ``against_voucher_type`` across the codebase. The grep of
# the snake_case literals actually used (sales_invoice, purchase_invoice,
# payment_entry, journal_entry, stock_entry, delivery_note, purchase_receipt,
# sales_order, purchase_order, credit_note, debit_note, expense_claim,
# stock_reconciliation, material_request, landed_cost_voucher) drives this set.
# A value normalizing into this set is the canonical key; anything else passes
# through unchanged.
_CANONICAL_VOUCHER_TYPES = {
    "sales_invoice",
    "purchase_invoice",
    "credit_note",
    "debit_note",
    "payment_entry",
    "journal_entry",
    "stock_entry",
    "delivery_note",
    "purchase_receipt",
    "sales_order",
    "purchase_order",
    "expense_claim",
    "stock_reconciliation",
    "material_request",
    "landed_cost_voucher",
}


def canonical_voucher_type(voucher_type):
    """Normalize a voucher_type to its canonical snake_case form.

    Maps doctype label forms ("Sales Invoice", "PURCHASE INVOICE",
    "stock-entry") to the canonical snake_case key. Case-insensitive; spaces and
    hyphens become underscores. Values that are already canonical pass through
    unchanged; genuinely-unknown types (after normalization) are returned as-is
    so advance / on-account / custom voucher types are never mangled.

    Examples:
        "Sales Invoice"    -> "sales_invoice"
        "sales_invoice"    -> "sales_invoice"
        "Purchase Invoice" -> "purchase_invoice"
        "Stock-Entry"      -> "stock_entry"
        "on_account"       -> "on_account"   (unknown, unchanged)
        None / ""          -> returned unchanged
    """
    if not voucher_type or not isinstance(voucher_type, str):
        return voucher_type
    normalized = voucher_type.strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in _CANONICAL_VOUCHER_TYPES:
        return normalized
    # Not a recognized canonical key after normalization: leave the original
    # value untouched rather than guess (advance / on-account / custom types).
    return voucher_type
