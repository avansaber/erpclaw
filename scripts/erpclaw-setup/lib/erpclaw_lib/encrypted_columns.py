"""Column-level encryption helpers.

Foundation tables with sensitive columns (SSN, bank account / routing
numbers) store ciphertext at rest. CRUD layers in domain action handlers
call encrypt_for_column / decrypt_for_column to round-trip values.

Encryption is AES-256-GCM via erpclaw_lib.crypto, keyed by the per-machine
master key from erpclaw_lib.master_key.
"""
from __future__ import annotations

from typing import Optional

from . import crypto
from .master_key import get_or_create_master_key

# Tables + columns whose values are encrypted at rest. Keep this list
# explicit so that future schema changes intentionally opt in.
ENCRYPTED_COLUMNS = {
    ("employee", "ssn"),
    ("employee_bank_account", "routing_number"),
    ("employee_bank_account", "account_number"),
}


def is_encrypted_column(table: str, column: str) -> bool:
    return (table, column) in ENCRYPTED_COLUMNS


def encrypt_for_column(value, table: str, column: str):
    """Encrypt a value if (table, column) is in the encrypted-columns registry."""
    if value is None:
        return None
    if not is_encrypted_column(table, column):
        return value
    mk = get_or_create_master_key()
    return crypto.encrypt_field(value, mk)


def decrypt_for_column(value, table: str, column: str):
    """Decrypt an `enc:v2:...` (or legacy `enc:...`) value if for an encrypted column.

    Pass-through for plaintext values to support pre-migration data and
    column-mismatched calls.
    """
    if value is None or not isinstance(value, str):
        return value
    if not is_encrypted_column(table, column):
        return value
    if not value.startswith("enc:"):
        return value  # plaintext (pre-migration row)
    mk = get_or_create_master_key()
    return crypto.decrypt_field(value, mk)
