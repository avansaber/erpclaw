"""ERPClaw cryptographic primitives.

Backed by the `cryptography` library (OpenSSL via cffi). Symmetric
encryption uses AES-256-GCM. Key derivation uses PBKDF2-HMAC-SHA256 at
600,000 iterations (OWASP 2024 recommendation).

Two file ciphertext formats are supported:

  ECRYPT02 (current):
    Streaming AES-256-GCM. Used for files of any size, including
    multi-GB backups. Plaintext is split into 1 MiB frames; each frame
    has its own GCM nonce + 16-byte tag. Header carries the KDF salt
    and (optionally) a wrapped copy of the column-encryption master key
    for cross-machine restore.

  ECRYPT01 (legacy v4.0.x):
    HMAC-SHA256-CTR construction. Decrypt-only path retained so users
    with existing v4.0.x encrypted backups can restore them. New
    encryption always uses ECRYPT02.

Field-level encryption (encrypt_field/decrypt_field) uses raw
AES-256-GCM with a per-call 12-byte random nonce. Used for short
columns (SSN, bank account number, routing number, etc.).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


# ---------------------------------------------------------------------------
# KDF
# ---------------------------------------------------------------------------

PBKDF2_ITERATIONS = 600_000  # OWASP 2024 minimum for SHA-256
SALT_LEN = 16
KEY_LEN = 32  # AES-256


def derive_key(passphrase: str, salt: bytes,
               iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """Derive a 32-byte AES-256 key from a passphrase via PBKDF2-HMAC-SHA256."""
    if isinstance(passphrase, bytes):
        pw = passphrase
    else:
        pw = passphrase.encode("utf-8")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(pw)


# ---------------------------------------------------------------------------
# File encryption: ECRYPT02 streaming AES-256-GCM
# ---------------------------------------------------------------------------

ECRYPT02_MAGIC = b"ECRYPT02"
ECRYPT01_MAGIC = b"ERPCLAW_ENC\x01"
CHUNK_SIZE = 1024 * 1024  # 1 MiB plaintext per frame
GCM_NONCE_LEN = 12
GCM_TAG_LEN = 16


def _pack_header_v2(salt: bytes, iterations: int,
                    nonce_prefix: bytes,
                    wrapped_master_key: Optional[bytes]) -> bytes:
    """Pack ECRYPT02 header.

    Layout:
      magic (8) || version (1) || iter_count (u32 BE) || salt_len (u8) || salt
       || nonce_prefix (8) || wrap_len (u16 BE) || wrapped_master_key (variable)
    """
    wrap = wrapped_master_key or b""
    return (
        ECRYPT02_MAGIC
        + b"\x02"
        + struct.pack(">I", iterations)
        + struct.pack(">B", len(salt))
        + salt
        + nonce_prefix
        + struct.pack(">H", len(wrap))
        + wrap
    )


def _unpack_header_v2(fh) -> tuple[int, bytes, bytes, bytes]:
    """Read ECRYPT02 header. Returns (iterations, salt, nonce_prefix, wrapped_key)."""
    magic = fh.read(len(ECRYPT02_MAGIC))
    if magic != ECRYPT02_MAGIC:
        raise ValueError(f"not an ECRYPT02 file (magic={magic!r})")
    version = fh.read(1)
    if version != b"\x02":
        raise ValueError(f"unsupported ECRYPT02 version: {version!r}")
    iterations = struct.unpack(">I", fh.read(4))[0]
    salt_len = struct.unpack(">B", fh.read(1))[0]
    salt = fh.read(salt_len)
    nonce_prefix = fh.read(8)
    wrap_len = struct.unpack(">H", fh.read(2))[0]
    wrapped = fh.read(wrap_len) if wrap_len else b""
    return iterations, salt, nonce_prefix, wrapped


def encrypt_file(input_path: str, output_path: str, passphrase: str,
                 wrapped_master_key: Optional[bytes] = None) -> dict:
    """Encrypt a file with AES-256-GCM streaming (ECRYPT02 format).

    `wrapped_master_key` (optional) is embedded in the header for
    cross-machine restore: a backup taken on machine A can be decrypted
    AND used to re-establish the column-encryption master key on
    machine B by passing the same passphrase.
    """
    salt = secrets.token_bytes(SALT_LEN)
    key = derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    nonce_prefix = secrets.token_bytes(8)

    original_size = os.path.getsize(input_path)
    with open(input_path, "rb") as fin, open(output_path, "wb") as fout:
        fout.write(_pack_header_v2(salt, PBKDF2_ITERATIONS, nonce_prefix,
                                    wrapped_master_key))
        chunk_index = 0
        while True:
            plaintext = fin.read(CHUNK_SIZE)
            # Detect end of stream
            peek = fin.read(1) if plaintext else b""
            is_last_byte = b"\x00" if peek else b"\x01"
            if peek:
                fin.seek(-1, 1)  # restore the peeked byte for next read
            if not plaintext and chunk_index > 0:
                # Already wrote the last frame on the previous iteration
                break
            nonce = nonce_prefix + struct.pack(">I", chunk_index)
            ct = aesgcm.encrypt(nonce, plaintext, is_last_byte)
            fout.write(struct.pack(">I", len(ct)))
            fout.write(is_last_byte)
            fout.write(ct)
            chunk_index += 1
            if is_last_byte == b"\x01":
                break

    encrypted_size = os.path.getsize(output_path)
    return {
        "format": "ECRYPT02",
        "original_size": original_size,
        "encrypted_size": encrypted_size,
        "iterations": PBKDF2_ITERATIONS,
    }


def decrypt_file(input_path: str, output_path: str, passphrase: str) -> dict:
    """Decrypt a file. Auto-detects ECRYPT02 vs legacy ECRYPT01 format."""
    with open(input_path, "rb") as fin:
        magic_peek = fin.read(len(ECRYPT02_MAGIC))
        fin.seek(0)
        if magic_peek == ECRYPT02_MAGIC:
            return _decrypt_ecrypt02(fin, output_path, passphrase)
        # Legacy
        return _decrypt_ecrypt01_legacy(input_path, output_path, passphrase)


def _decrypt_ecrypt02(fin, output_path: str, passphrase: str) -> dict:
    iterations, salt, nonce_prefix, wrapped = _unpack_header_v2(fin)
    key = derive_key(passphrase, salt, iterations)
    aesgcm = AESGCM(key)

    chunk_index = 0
    written = 0
    with open(output_path, "wb") as fout:
        while True:
            len_bytes = fin.read(4)
            if not len_bytes:
                break
            ct_len = struct.unpack(">I", len_bytes)[0]
            is_last = fin.read(1)
            ct = fin.read(ct_len)
            nonce = nonce_prefix + struct.pack(">I", chunk_index)
            pt = aesgcm.decrypt(nonce, ct, is_last)
            fout.write(pt)
            written += len(pt)
            chunk_index += 1
            if is_last == b"\x01":
                break
    return {
        "format": "ECRYPT02",
        "decrypted_size": written,
        "wrapped_master_key": wrapped,
    }


def _decrypt_ecrypt01_legacy(input_path: str, output_path: str,
                              passphrase: str) -> dict:
    """Legacy decrypt for v4.0.x ECRYPT01 backups (HMAC-SHA256-CTR)."""
    with open(input_path, "rb") as fin:
        data = fin.read()
    if not data.startswith(ECRYPT01_MAGIC):
        raise ValueError("not a recognized ERPClaw encrypted file")
    body = data[len(ECRYPT01_MAGIC):]
    salt = body[:SALT_LEN]
    iv = body[SALT_LEN:SALT_LEN + 16]
    mac = body[SALT_LEN + 16:SALT_LEN + 16 + 32]
    ciphertext = body[SALT_LEN + 16 + 32:]

    # Legacy used 480,000 iterations
    key = derive_key(passphrase, salt, iterations=480_000)
    expected = hmac.new(key, iv + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("HMAC mismatch — file corrupted or wrong passphrase")

    plaintext = bytearray()
    counter = 0
    pos = 0
    while pos < len(ciphertext):
        counter_bytes = struct.pack(">Q", counter)
        keystream = hmac.new(key, iv + counter_bytes, hashlib.sha256).digest()
        block = ciphertext[pos:pos + 32]
        for i, b in enumerate(block):
            plaintext.append(b ^ keystream[i])
        pos += 32
        counter += 1

    with open(output_path, "wb") as fout:
        fout.write(plaintext)
    return {"format": "ECRYPT01_LEGACY", "decrypted_size": len(plaintext)}


def is_encrypted_backup(file_path: str) -> bool:
    """Return True if file is an ERPClaw encrypted backup (any format)."""
    try:
        with open(file_path, "rb") as fh:
            head = fh.read(max(len(ECRYPT02_MAGIC), len(ECRYPT01_MAGIC)))
        return head.startswith(ECRYPT02_MAGIC) or head.startswith(ECRYPT01_MAGIC)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Field encryption: AES-256-GCM raw
# ---------------------------------------------------------------------------

FIELD_PREFIX = "enc:v2:"


def encrypt_field(value, key: bytes):
    """Encrypt a short string with AES-256-GCM. Returns base64 `enc:v2:...`.

    Pass-through if value is None.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        plaintext = value
    else:
        plaintext = str(value).encode("utf-8")
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(GCM_NONCE_LEN)
    ct = aesgcm.encrypt(nonce, plaintext, None)
    return FIELD_PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_field(value, key: bytes):
    """Decrypt `enc:v2:...` (current) or `enc:...` (legacy). Pass-through plaintext."""
    if value is None or not isinstance(value, str):
        return value
    if value.startswith(FIELD_PREFIX):
        payload = base64.b64decode(value[len(FIELD_PREFIX):])
        nonce = payload[:GCM_NONCE_LEN]
        ct = payload[GCM_NONCE_LEN:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
    if value.startswith("enc:"):
        return _decrypt_field_legacy(value, key)
    return value


def _decrypt_field_legacy(value: str, key: bytes) -> str:
    """v4.0.x encrypt_field format: `enc:<b64(iv||hmac||ct)>`."""
    payload = base64.b64decode(value[4:])
    iv = payload[:16]
    mac = payload[16:48]
    ct = payload[48:]
    expected = hmac.new(key, iv + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("HMAC mismatch in legacy field decrypt")
    pt = bytearray()
    counter = 0
    pos = 0
    while pos < len(ct):
        counter_bytes = struct.pack(">Q", counter)
        keystream = hmac.new(key, iv + counter_bytes, hashlib.sha256).digest()
        block = ct[pos:pos + 32]
        for i, b in enumerate(block):
            pt.append(b ^ keystream[i])
        pos += 32
        counter += 1
    return pt.decode("utf-8")


# ---------------------------------------------------------------------------
# Master key wrap / unwrap
# ---------------------------------------------------------------------------

def wrap_master_key(master_key: bytes, passphrase: str) -> bytes:
    """Encrypt master key with passphrase-derived KEK for portable backup carrying."""
    salt = secrets.token_bytes(SALT_LEN)
    kek = derive_key(passphrase, salt)
    aesgcm = AESGCM(kek)
    nonce = secrets.token_bytes(GCM_NONCE_LEN)
    ct = aesgcm.encrypt(nonce, master_key, None)
    return salt + nonce + ct


def unwrap_master_key(wrapped: bytes, passphrase: str) -> bytes:
    """Inverse of wrap_master_key."""
    salt = wrapped[:SALT_LEN]
    nonce = wrapped[SALT_LEN:SALT_LEN + GCM_NONCE_LEN]
    ct = wrapped[SALT_LEN + GCM_NONCE_LEN:]
    kek = derive_key(passphrase, salt)
    aesgcm = AESGCM(kek)
    return aesgcm.decrypt(nonce, ct, None)
