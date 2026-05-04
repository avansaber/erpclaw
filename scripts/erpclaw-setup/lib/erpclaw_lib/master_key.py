"""Master key management for ERPClaw column-level encryption + credentials.

The master key is a 32-byte AES-256 key generated once per machine on first
use. Stored at ~/.config/erpclaw/master.key with mode 0600. The owner of
the runtime user account is responsible for full-disk encryption / file
permissions; this module enforces 0600 on the file and on its parent dir.

For cross-machine restore, the master key is wrapped with a passphrase
and embedded in the ECRYPT02 backup header (see crypto.wrap_master_key).
"""
from __future__ import annotations

import os
import secrets
import stat

CONFIG_DIR = os.path.expanduser("~/.config/erpclaw")
MASTER_KEY_PATH = os.path.join(CONFIG_DIR, "master.key")


def _ensure_config_dir() -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def get_or_create_master_key() -> bytes:
    """Return the 32-byte master key, generating it on first call.

    Idempotent. Subsequent calls return the same key. File mode is forced
    to 0600 every call.
    """
    _ensure_config_dir()
    if os.path.isfile(MASTER_KEY_PATH):
        with open(MASTER_KEY_PATH, "rb") as fh:
            mk = fh.read()
        if len(mk) != 32:
            raise ValueError(
                f"master key file has wrong length ({len(mk)}); expected 32. "
                f"Refusing to overwrite. Inspect {MASTER_KEY_PATH} manually."
            )
        try:
            os.chmod(MASTER_KEY_PATH, 0o600)
        except OSError:
            pass
        return mk
    # First-time generation
    mk = secrets.token_bytes(32)
    fd = os.open(MASTER_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, mk)
    finally:
        os.close(fd)
    return mk


def import_master_key(mk: bytes) -> None:
    """Install a master key (e.g. unwrapped from a backup on another machine).

    Refuses to overwrite an existing master key file unless it's identical.
    """
    if len(mk) != 32:
        raise ValueError("master key must be exactly 32 bytes")
    _ensure_config_dir()
    if os.path.isfile(MASTER_KEY_PATH):
        with open(MASTER_KEY_PATH, "rb") as fh:
            existing = fh.read()
        if existing == mk:
            return
        raise FileExistsError(
            f"master key already exists at {MASTER_KEY_PATH} and differs from "
            f"the imported key. Move or delete the existing file before importing."
        )
    fd = os.open(MASTER_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, mk)
    finally:
        os.close(fd)


def master_key_exists() -> bool:
    return os.path.isfile(MASTER_KEY_PATH)
