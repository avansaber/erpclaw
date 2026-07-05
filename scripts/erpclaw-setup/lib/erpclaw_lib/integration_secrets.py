"""At-rest encryption for integration API secrets (Stripe keys, Shopify tokens).

The Stripe and Shopify addons store per-account API credentials in DB columns
(``stripe_account.restricted_key_enc`` / ``webhook_secret_enc``,
``shopify_account.access_token_enc`` / ``hmac_secret_enc``). This module is the
single source of truth for encrypting those columns; both addons delegate here.

Current format: AES-256-GCM via :func:`crypto.encrypt_field`, keyed by the
per-machine master key (ciphertext prefixed ``enc:v2:``). This is the same
primitive that already backs foundation column encryption (SSN, bank numbers)
and the credentials store, so integration secrets now get real authenticated
encryption instead of the pre-M31 bespoke cipher.

Legacy read-back: before M31, both addons used a home-directory-salted XOR
cipher that produced a bare base64 string with no prefix. Existing installs
still hold those values, so :func:`decrypt_secret` transparently reads them
(the ECRYPT01 precedent — decrypt old, always write new). A bare base64 value
can never begin with ``"enc:"`` (``:`` is outside the base64 alphabet), so the
format discriminator is unambiguous. Values are upgraded to GCM the next time
the owning column is re-written (add/update-account, reconnect).

Empty/blank input passes through as ``""`` unchanged: the addons store an empty
string to mean "no key", and both the historical and new contracts preserve that.
"""
from __future__ import annotations

import base64
import hashlib
import os

from . import crypto
from .master_key import get_or_create_master_key


def _legacy_xor_decrypt(ciphertext: str) -> str:
    """Decrypt a pre-M31 XOR-with-home-salt value (bare base64, no prefix).

    Byte-for-byte the inverse of the retired ``encrypt_key``/``encrypt_token``:
    salt = ``sha256(expanduser("~"))``, XOR keystream, base64 transport.
    """
    salt = hashlib.sha256(os.path.expanduser("~").encode()).digest()
    decoded = base64.b64decode(ciphertext.encode())
    decrypted = bytes(b ^ salt[i % len(salt)] for i, b in enumerate(decoded))
    return decrypted.decode()


def encrypt_secret(plaintext):
    """Encrypt an integration secret for at-rest storage.

    Returns ``""`` for empty/falsy input (the addons store "" for "no key").
    Otherwise returns an ``enc:v2:...`` AES-256-GCM ciphertext.
    """
    if not plaintext:
        return ""
    mk = get_or_create_master_key()
    return crypto.encrypt_field(plaintext, mk)


def decrypt_secret(ciphertext):
    """Decrypt a stored integration secret.

    Reads both the current ``enc:v2:`` GCM format and legacy pre-M31 XOR-salt
    values, so existing installs need no re-encryption. Empty/falsy input
    returns ``""``.
    """
    if not ciphertext:
        return ""
    if ciphertext.startswith("enc:"):
        mk = get_or_create_master_key()
        return crypto.decrypt_field(ciphertext, mk)
    return _legacy_xor_decrypt(ciphertext)
