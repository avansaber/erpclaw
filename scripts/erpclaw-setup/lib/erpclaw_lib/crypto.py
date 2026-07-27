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
import stat
import struct
import sys
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


# ---------------------------------------------------------------------------
# Pre-master-key-load environment sanity check (M36 R-b, #7071-class)
# ---------------------------------------------------------------------------
#
# The S0c probe (planning/HERMES_S0_PROBE_RESULTS_2026-06-14.md) established
# that skill subprocesses inherit the parent interpreter environment by
# standard subprocess inheritance on every runtime (OpenClaw, Hermes venv —
# no docker shim in the loop). A #7071-class attack therefore does not need
# to touch ERPClaw code at all: poisoning PYTHONPATH / PYTHONSTARTUP /
# sitecustomize with a module in a world-writable or tmp location lets
# injected code run inside the very process that decrypts the master key.
# This check inspects the interpreter environment for exactly those
# indicators BEFORE the key is loaded or a wrapped key is decrypted.
#
# Indicator list (derived from the S0c analysis):
#   1. PYTHONPATH entries that resolve under a shared tmp root (/tmp,
#      /var/tmp, /dev/shm, $TMPDIR) or into a world-writable directory —
#      any local user could plant a shadowing module there.
#   2. PYTHONSTARTUP set at all — arbitrary code at interpreter startup.
#   3. A sitecustomize/usercustomize module imported from such a path —
#      the interpreter already ran code from an attacker-writable location.
#
# DEFAULT = WARN LOUDLY to stderr and continue; ERPCLAW_STRICT_ENV=1
# upgrades to refuse (RuntimeError before any key material is touched).
# Why warn-by-default: ERPClaw is self-hosted and single-user by design —
# the same person owns the shell and the books, and developers legitimately
# set PYTHONPATH. A hard fail on a merely-unusual environment would brick
# every key-backed action (HR --ssn, credentials, encrypted backups) for
# that legitimate majority; a loud stderr warning preserves the signal
# without taking the user's own data hostage. Hardened multi-user installs
# opt into refusal with one environment variable. Runtime-agnostic, stdlib
# only (os/sys/stat), zero new dependencies.

ERPCLAW_STRICT_ENV_VAR = "ERPCLAW_STRICT_ENV"

_TMP_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")

# Warn once per process (key loads happen per-field in encrypted-column
# flows; the scan is cheap but the stderr noise would not be).
_ENV_WARNING_EMITTED = False


def _is_world_writable_dir(path: str) -> bool:
    """True if path is a directory writable by 'other' (mode o+w)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and bool(st.st_mode & stat.S_IWOTH)


def _under_tmp_root(path: str) -> bool:
    """True if path resolves under a shared tmp root (incl. $TMPDIR)."""
    real = os.path.realpath(path)
    roots = list(_TMP_ROOTS)
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.append(os.path.realpath(tmpdir))
    for root in roots:
        real_root = os.path.realpath(root)
        if real == real_root or real.startswith(real_root + os.sep):
            return True
    return False


def scan_environment_injection_indicators() -> list:
    """Scan the interpreter environment for #7071-class injection indicators.

    Pure inspection — never mutates anything. Returns a list of human-readable
    finding strings (empty = clean).
    """
    findings = []

    # 1. Suspicious PYTHONPATH entries (tmp-rooted or world-writable).
    pythonpath = os.environ.get("PYTHONPATH", "")
    for entry in pythonpath.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        expanded = os.path.expanduser(entry)
        if _under_tmp_root(expanded):
            findings.append(
                f"PYTHONPATH entry under a shared tmp path: {entry!r} — "
                "injected modules there can shadow stdlib/erpclaw_lib inside "
                "the key-decrypting process")
        elif _is_world_writable_dir(expanded):
            findings.append(
                f"PYTHONPATH entry is a world-writable directory: {entry!r} — "
                "any local user can plant a shadowing module there")

    # 2. PYTHONSTARTUP set (arbitrary code at interpreter startup).
    startup = os.environ.get("PYTHONSTARTUP")
    if startup:
        findings.append(
            f"PYTHONSTARTUP is set ({startup!r}) — the interpreter may execute "
            "arbitrary startup code in credential-handling processes")

    # 3. sitecustomize / usercustomize loaded from a tmp / world-writable path.
    for mod_name in ("sitecustomize", "usercustomize"):
        mod = sys.modules.get(mod_name)
        mod_file = getattr(mod, "__file__", None) if mod else None
        if not mod_file:
            continue
        mod_dir = os.path.dirname(os.path.realpath(mod_file))
        if _under_tmp_root(mod_file) or _is_world_writable_dir(mod_dir):
            findings.append(
                f"{mod_name} was imported from a suspicious location "
                f"({mod_file!r}) — startup-hook code already ran in this "
                "process")

    return findings


def check_environment_before_key_load(strict: Optional[bool] = None) -> list:
    """Run the #7071-class scan; warn (default) or refuse (strict) on findings.

    Call BEFORE loading, generating, importing, or unwrapping the master key.
    `strict=None` reads ERPCLAW_STRICT_ENV (value "1"/"true"/"yes" = strict).
    Returns the findings list. In strict mode any finding raises RuntimeError
    before key material is touched; in default mode the findings are written
    to stderr once per process and execution continues (rationale above).
    """
    global _ENV_WARNING_EMITTED
    findings = scan_environment_injection_indicators()
    if not findings:
        return findings

    if strict is None:
        strict = os.environ.get(ERPCLAW_STRICT_ENV_VAR, "").strip().lower() in (
            "1", "true", "yes")

    if strict:
        raise RuntimeError(
            "Refusing to load the ERPClaw master key: suspicious interpreter "
            "environment (" + "; ".join(findings) + "). Unset "
            f"{ERPCLAW_STRICT_ENV_VAR} to downgrade this to a warning, or fix "
            "the environment.")

    if not _ENV_WARNING_EMITTED:
        print(
            "WARNING [erpclaw]: suspicious interpreter environment detected "
            "before master-key load:\n  - " + "\n  - ".join(findings) +
            f"\n  Continuing (self-hosted default). Set {ERPCLAW_STRICT_ENV_VAR}=1 "
            "to refuse instead.",
            file=sys.stderr)
        _ENV_WARNING_EMITTED = True
    return findings


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
    """Legacy decrypt for v3.5.x / v4.0.x ECRYPT01 backups.

    Format (mirrors the v4.0.2 encrypt_file at git tag v4.0.2):
        magic (12) || salt (16) || iv (16) || ciphertext (variable) || mac (32)

    Uses encrypt-then-MAC with separate mac_key
    (derive_key(passphrase, salt + b"mac", iterations=1000)). PBKDF2
    iter count for the encryption key is 480,000 (legacy default).
    Stream cipher is HMAC-SHA256-CTR with little-endian counter || iv[:8].
    """
    with open(input_path, "rb") as fin:
        data = fin.read()
    if not data.startswith(ECRYPT01_MAGIC):
        raise ValueError("not a recognized ERPClaw encrypted file")

    offset = len(ECRYPT01_MAGIC)
    salt = data[offset:offset + 16]; offset += 16
    iv = data[offset:offset + 16]; offset += 16
    mac = data[-32:]
    ciphertext = data[offset:-32]

    # Legacy used 480,000 iterations for the encryption key, separate
    # 1,000-iter key for the MAC.
    key = derive_key(passphrase, salt, iterations=480_000)
    mac_key = derive_key(passphrase, salt + b"mac", iterations=1_000)

    expected = hmac.new(mac_key, salt + iv + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("HMAC mismatch — file corrupted or wrong passphrase")

    # CTR-mode stream cipher: keystream = HMAC(key, struct.pack("<Q", counter) + iv[:8])
    plaintext = bytearray()
    counter = 0
    pos = 0
    while pos < len(ciphertext):
        counter_bytes = struct.pack("<Q", counter) + iv[:8]
        keystream = hmac.new(key, counter_bytes, hashlib.sha256).digest()
        block = ciphertext[pos:pos + 32]
        for i, b in enumerate(block):
            plaintext.append(b ^ keystream[i])
        counter += 1
        pos += 32

    plaintext = bytes(plaintext[:len(ciphertext)])
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
    """v3.5.x / v4.0.x encrypt_field format: `enc:<b64(iv || ciphertext)>`.

    No HMAC at field level (fields were unauthenticated in legacy). 16-byte
    IV followed directly by ciphertext. Stream cipher is HMAC-SHA256-CTR
    matching the legacy file format (little-endian counter || iv[:8]).
    """
    raw = base64.b64decode(value[4:])
    iv = raw[:16]
    ct = raw[16:]
    pt = bytearray()
    counter = 0
    pos = 0
    while pos < len(ct):
        counter_bytes = struct.pack("<Q", counter) + iv[:8]
        keystream = hmac.new(key, counter_bytes, hashlib.sha256).digest()
        block = ct[pos:pos + 32]
        for i, b in enumerate(block):
            pt.append(b ^ keystream[i])
        counter += 1
        pos += 32
    return bytes(pt[:len(ct)]).decode("utf-8")


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
    """Inverse of wrap_master_key.

    Decrypts master-key material, so the #7071-class environment check runs
    first (M36 R-b) — see check_environment_before_key_load.
    """
    check_environment_before_key_load()
    salt = wrapped[:SALT_LEN]
    nonce = wrapped[SALT_LEN:SALT_LEN + GCM_NONCE_LEN]
    ct = wrapped[SALT_LEN + GCM_NONCE_LEN:]
    kek = derive_key(passphrase, salt)
    aesgcm = AESGCM(kek)
    return aesgcm.decrypt(nonce, ct, None)
