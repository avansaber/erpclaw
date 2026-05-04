"""Credential management for ERPClaw integrations.

Replaces the v4.0.x pattern of accepting `--api-key` shell flags. Credentials
are now stored in an encrypted file at ~/.config/erpclaw/credentials.json.enc
(mode 0600). Encryption uses the master key from master_key.py, AES-256-GCM
via crypto.encrypt_field/decrypt_field.

Public API:
    set_credential(integration: str, value: str) -> None
    get_credential(integration: str) -> Optional[str]
    list_credentials() -> list[str]   # names only, never values
    delete_credential(integration: str) -> bool
"""
from __future__ import annotations

import json
import os
from typing import Optional

from . import crypto
from .master_key import CONFIG_DIR, get_or_create_master_key

CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json.enc")


def _load() -> dict:
    """Load credentials dict; returns {} if file missing."""
    if not os.path.isfile(CREDENTIALS_PATH):
        return {}
    mk = get_or_create_master_key()
    with open(CREDENTIALS_PATH, "r") as fh:
        encrypted_blob = fh.read()
    if not encrypted_blob.strip():
        return {}
    plaintext_json = crypto.decrypt_field(encrypted_blob, mk)
    return json.loads(plaintext_json)


def _save(data: dict) -> None:
    mk = get_or_create_master_key()
    plaintext_json = json.dumps(data, sort_keys=True)
    encrypted_blob = crypto.encrypt_field(plaintext_json, mk)
    os.makedirs(os.path.dirname(CREDENTIALS_PATH), mode=0o700, exist_ok=True)
    fd = os.open(CREDENTIALS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encrypted_blob.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(CREDENTIALS_PATH, 0o600)
    except OSError:
        pass


def set_credential(integration: str, value: str) -> None:
    """Store an integration credential. Overwrites any existing entry for that name."""
    if not integration:
        raise ValueError("integration name is required")
    data = _load()
    data[integration] = value
    _save(data)


def get_credential(integration: str) -> Optional[str]:
    """Return the stored credential, or None if not set."""
    if not integration:
        return None
    data = _load()
    return data.get(integration)


def list_credentials() -> list:
    """Return integration names. Never returns the values themselves."""
    return sorted(_load().keys())


def delete_credential(integration: str) -> bool:
    """Remove a credential. Returns True if deleted, False if not present."""
    data = _load()
    if integration not in data:
        return False
    del data[integration]
    _save(data)
    return True
