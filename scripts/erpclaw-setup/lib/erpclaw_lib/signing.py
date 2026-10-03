"""Registry signature verification for foundation reconciliation.

The published `module_registry.json` is signed with ed25519. The public key
is embedded here. Reconciliation refuses to trust an unsigned, tampered, or
downgraded registry.

Trust root: ed25519 keypair held by the publisher (Nik). The key list below
supports rotation: new key added with valid_from; old key kept for grace
period; the verifier accepts any currently-valid key.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

__all__ = [
    "TrustedKey",
    "TRUSTED_KEYS",
    "verify_registry_signature",
    "fingerprint",
    "REGISTRY_VERSION_FIELD",
    "SIGNED_AT_FIELD",
    "RegistryManifestError",
    "validate_registry_manifests",
]


class RegistryManifestError(ValueError):
    """A signed registry has an unsafe or unverifiable file manifest."""


@dataclass(frozen=True)
class TrustedKey:
    """A public key the verifier accepts as a valid signer.

    public_key_hex: 64 hex chars (32 raw bytes) of an ed25519 public key.
    valid_until: ISO 8601 date or None for indefinite. Keys past their
        valid_until are NOT accepted.
    label: human-friendly identifier for logging / fingerprint output.
    """

    public_key_hex: str
    valid_until: Optional[str]
    label: str


# Production trust root.
# Fingerprint d471:335b:0e4d:75ce — generated 2026-05-04 for v4.1.6.
TRUSTED_KEYS: tuple[TrustedKey, ...] = (
    TrustedKey(
        public_key_hex="d471335b0e4d75ce9a4cc58e34446bbfdd1c1fd77fbe2d73e300c3850c0827c5",
        valid_until=None,
        label="erpclaw-foundation-signer-2026-05-04",
    ),
)


REGISTRY_VERSION_FIELD = "registry_version"
SIGNED_AT_FIELD = "signed_at"


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GITHUB_RE = re.compile(r"avansaber/[A-Za-z0-9._-]+")


def validate_registry_manifests(registry: dict) -> None:
    """Fail unless every module has a nonempty, safe SHA-256 manifest.

    A valid signature authenticates bytes; it does not make their structure
    safe to consume as filesystem paths. Mutation and release paths call this
    immediately after signature verification and before using any repository
    coordinate, path, or digest from the payload.
    """
    if not isinstance(registry, dict):
        raise RegistryManifestError("registry root is not an object")

    modules = registry.get("modules")
    if isinstance(modules, dict):
        entries = list(modules.items())
    elif isinstance(modules, list):
        entries = []
        seen = set()
        for index, info in enumerate(modules):
            if not isinstance(info, dict):
                raise RegistryManifestError(
                    f"modules[{index}] is not an object"
                )
            name = info.get("name")
            if not isinstance(name, str) or not name:
                raise RegistryManifestError(
                    f"modules[{index}] has no nonempty string name"
                )
            if name in seen:
                raise RegistryManifestError(f"duplicate module name: {name}")
            seen.add(name)
            entries.append((name, info))
    else:
        raise RegistryManifestError("registry.modules is not an object or list")

    if not entries:
        raise RegistryManifestError("registry.modules is empty")

    for name, info in entries:
        if not isinstance(name, str) or not name:
            raise RegistryManifestError("module name is not a nonempty string")
        if not isinstance(info, dict):
            raise RegistryManifestError(f"{name}: module entry is not an object")

        github = info.get("github", info.get("github_repo"))
        if not isinstance(github, str) or _GITHUB_RE.fullmatch(github) is None:
            raise RegistryManifestError(
                f"{name}: unsafe or unsupported github coordinate {github!r}"
            )
        subdir = info.get("subdir")
        if subdir is not None:
            if (
                not isinstance(subdir, str)
                or not subdir
                or "\\" in subdir
                or "\x00" in subdir
            ):
                raise RegistryManifestError(
                    f"{name}: unsafe subdir coordinate {subdir!r}"
                )
            subdir_path = PurePosixPath(subdir)
            if (
                subdir_path.is_absolute()
                or subdir_path.as_posix() != subdir
                or any(part in ("", ".", "..") for part in subdir_path.parts)
            ):
                raise RegistryManifestError(
                    f"{name}: subdir is not normalized and relative: {subdir!r}"
                )

        manifest = info.get("files_sha256")
        if not isinstance(manifest, dict) or not manifest:
            raise RegistryManifestError(
                f"{name}: files_sha256 must be a nonempty object"
            )

        for rel, digest in manifest.items():
            if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
                raise RegistryManifestError(
                    f"{name}: unsafe manifest path {rel!r}"
                )
            path = PurePosixPath(rel)
            parts = path.parts
            if (
                path.is_absolute()
                or path.as_posix() != rel
                or any(part in ("", ".", "..") for part in parts)
                or (parts and parts[0].endswith(":"))
            ):
                raise RegistryManifestError(
                    f"{name}: manifest path is not normalized and relative: {rel!r}"
                )
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise RegistryManifestError(
                    f"{name}: invalid SHA-256 digest for {rel!r}"
                )


def fingerprint(public_key_hex: str) -> str:
    """Short human-readable fingerprint of an ed25519 public key.

    Returns first 16 hex chars in 4:4:4:4 colon format. Use for CHANGELOG
    documentation and out-of-band verification.
    """
    h = public_key_hex.lower()
    return f"{h[0:4]}:{h[4:8]}:{h[8:12]}:{h[12:16]}"


def verify_registry_signature(
    registry_bytes: bytes,
    signature_hex: str,
    *,
    accepted_keys: tuple[TrustedKey, ...] = TRUSTED_KEYS,
    today_iso: Optional[str] = None,
) -> TrustedKey:
    """Verify ed25519 signature against registry bytes.

    Returns the TrustedKey that successfully verified, or raises
    InvalidSignature.

    today_iso: ISO date for valid_until comparison; defaults to current UTC.
        Tests inject deterministic dates.
    """
    if not signature_hex:
        raise InvalidSignature("empty signature")
    try:
        sig_bytes = bytes.fromhex(signature_hex.strip())
    except ValueError as e:
        raise InvalidSignature(f"signature is not valid hex: {e}")
    if len(sig_bytes) != 64:
        raise InvalidSignature(f"ed25519 signature must be 64 bytes, got {len(sig_bytes)}")

    if today_iso is None:
        from datetime import datetime, timezone
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    last_err: Optional[Exception] = None
    for trusted in accepted_keys:
        if trusted.valid_until is not None and today_iso > trusted.valid_until:
            continue
        try:
            pub_bytes = bytes.fromhex(trusted.public_key_hex)
        except ValueError:
            continue
        if len(pub_bytes) != 32:
            continue
        try:
            pk = Ed25519PublicKey.from_public_bytes(pub_bytes)
            pk.verify(sig_bytes, registry_bytes)
            return trusted
        except (InvalidSignature, ValueError) as e:
            last_err = e
            continue

    raise InvalidSignature(
        f"no trusted key verified the signature; last error: {last_err}"
    )
