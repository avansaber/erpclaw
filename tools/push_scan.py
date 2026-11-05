#!/usr/bin/env python3
"""Object-complete, immutable-snapshot push scanner for ERPClaw.

Usage: push_scan.py [--policy <file.json>] <remote-name> <local-branch>

Exit codes:
  0  clean
  1  HARD BLOCK: the push must be refused
  2  warnings only
  3  invalid or incomplete measurement: this result authorizes nothing

Every committed version introduced by the push is scanned, including a blob
that is added and deleted before the pushed tip; filenames stay NUL-safe; the
scan refuses shallow history and graft overlays and checks parent closure.

Modes, chosen from the push URL and the policy file:
  private  the repository is listed in policy "private_repos" and not in
           "clean_history_repos": secret and PII shapes block.
  clean    listed in "clean_history_repos" (private now, public later): as
           public.
  public   anything else, and every repository when no policy is given:
           secret and PII shapes block, and so do internal-path and
           plan-artifact shapes plus every policy "engine_forbidden" pattern,
           in file contents, file names and whole commit messages (trailers
           included), and every commit needs a "Plan: M<n>-T<n>" trailer.

The policy file keeps deployment-specific values (private repository names,
internal codenames, host addresses) out of this source, which is itself public.

Contract (what a CLEAN result means, and what it does not):
  - Secrets: whatever gitleaks GITLEAKS_VERSION finds with its default rules
    and allowlist plus tools/gitleaks.toml, in every introduced blob version
    (each text view: Latin-1, and UTF-16, UTF-32 and NUL-stripped when the
    bytes hold NULs), every commit message and metadata block, every
    committed path name, and the push URL. Inline "gitleaks:allow" markers
    are ignored. Without that exact gitleaks the scan is INVALID.
  - PII: US SSN and the government identifiers in PII_PATTERNS.
  - Policy (this scanner's own job): plan-artifact and internal-path shapes,
    the policy file's forbidden patterns, the Plan trailer, credential-like
    file names, opaque archives and LFS pointers, and the push binding.
  - Not guaranteed: what gitleaks's rules do not cover (a low-entropy
    password, a secret split across fields or encoded), and formats neither
    can read (images, PDFs). High-entropy strings, unfamiliar emails and
    public IP addresses only WARN.
  - So the scanner is the last line, not the first: secrets are kept out of
    the workspace entirely (OS keychain or environment), and that is the
    control a CLEAN result rests on.

No matched secret or PII value is printed. Findings carry the immutable commit,
JSON-escaped path, line, label, and length. Sensitive paths are fully redacted;
no digest is emitted because identifiers with a small value space are cheaply
dictionary-reversible even when hashed.
"""

from __future__ import annotations

import bisect
import io
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EXIT_CLEAN = 0
EXIT_HARD_BLOCK = 1
EXIT_WARN = 2
EXIT_INVALID = 3


_GIT_ENVIRONMENT_BLOCKLIST = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_OBJECT_DIRECTORY_RELATIVE",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_EXEC_PATH",
        "GIT_PROXY_COMMAND",
        "GIT_WORK_TREE",
    }
)
_GIT_ENVIRONMENT_BLOCK_PREFIXES = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
)
_GIT_TRANSPORT_OVERRIDE_ENV = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "GIT_EXEC_PATH",
        "GIT_PROXY_COMMAND",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_SSL_NO_VERIFY",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
# Config that would make a later `git push` use another transport, helper,
# destination ref or extra refs (tags) than the ones this scan measured.
_GIT_TRANSPORT_CONFIG_PATTERN = (
    r"^(core\.sshcommand|url\..*\.(insteadof|pushinsteadof)|"
    r"http\..*|remote\..*\.(proxy|vcs|push|receivepack|uploadpack|mirror)|"
    r"push\.(followtags|pushoption|recursesubmodules))$"
)
_PINNED_GIT_SSH_COMMAND = (
    "/usr/bin/ssh -F /dev/null -o BatchMode=yes -o ClearAllForwardings=yes "
    "-o PermitLocalCommand=no -o ProxyCommand=none -o ProxyJump=none "
    "-o CanonicalizeHostname=no -o HostName=github.com "
    "-o HostKeyAlias=github.com -o StrictHostKeyChecking=yes"
)
_PINNED_GIT_NETWORK_OPTIONS = (
    "-c",
    f"core.sshCommand={_PINNED_GIT_SSH_COMMAND}",
    "-c",
    "ssh.variant=ssh",
    "-c",
    "http.proxy=",
    "-c",
    "http.sslVerify=true",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=always",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "protocol.ssh.allow=always",
)


class ScanInvalid(RuntimeError):
    """The scanner could not prove what object/range it inspected."""


@dataclass(frozen=True)
class Version:
    commit: str
    path: str
    oid: str
    data: bytes


@dataclass(frozen=True)
class ExternalPayload:
    commit: str
    path: str
    oid: str


# PII shapes. Secrets are not matched here: they come from gitleaks (below).
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "government registration number (A-prefixed)",
        re.compile(
            r"(?<![A-Za-z0-9])A(?:[0-9]{8,9}|-[0-9]{3}-[0-9]{3}-[0-9]{3})(?![A-Za-z0-9])"
        ),
    ),
    (
        "government case receipt number",
        re.compile(
            r"(?<![A-Za-z0-9])(?:EAC|WAC|LIN|SRC|MSC|IOE|NBC|YSC)-?[0-9]{10}(?![A-Za-z0-9])"
        ),
    ),
    (
        "government record number (N-prefixed)",
        re.compile(r"(?<![A-Za-z0-9])N-?[0-9]{10}(?![A-Za-z0-9])"),
    ),
)

# Secrets detection is gitleaks at a pinned version: its default rules and
# allowlist plus the few shapes in tools/gitleaks.toml. It runs once per scan
# over every introduced blob version (each text view), every commit message
# and metadata block, and every committed path name. Inline "gitleaks:allow"
# markers are ignored. A missing or different gitleaks makes the scan INVALID.
GITLEAKS_VERSION = "8.30.1"
GITLEAKS_CONFIG = Path(__file__).resolve().with_name("gitleaks.toml")


@dataclass(frozen=True)
class SecretFinding:
    index: int   # position of the item in the list given to gitleaks_scan
    line: int
    rule: str
    length: int


def _gitleaks_binary() -> str:
    path = os.environ.get("ERPCLAW_GITLEAKS") or shutil.which("gitleaks")
    if not path:
        raise ScanInvalid("gitleaks is not installed (set ERPCLAW_GITLEAKS); secrets cannot be checked")
    try:
        proc = subprocess.run([path, "version"], capture_output=True, text=True)
    except OSError as exc:
        raise ScanInvalid(f"gitleaks at the given path cannot run ({exc.strerror})") from None
    found = proc.stdout.strip()
    if proc.returncode != 0 or found != GITLEAKS_VERSION:
        raise ScanInvalid(f"gitleaks {GITLEAKS_VERSION} is required, found {found or 'none'}")
    return path


def gitleaks_scan(items: list[bytes]) -> list[SecretFinding]:
    """Secrets in each item, by gitleaks. One run for all items; every text
    view of an item is its own file, so a UTF-16 secret is read as text."""
    binary = _gitleaks_binary()
    with tempfile.TemporaryDirectory(prefix="push-scan-") as tmp:
        root = Path(tmp) / "items"
        root.mkdir()
        owner: dict[str, int] = {}
        for i, data in enumerate(items):
            for j, view in enumerate(_text_views(data)):
                name = f"{i:07d}-{j}.txt"
                (root / name).write_text(view, encoding="utf-8", errors="surrogatepass")
                owner[name] = i
        report = Path(tmp) / "report.json"
        proc = subprocess.run(
            [binary, "dir", str(root), "--config", str(GITLEAKS_CONFIG), "--no-banner", "--redact",
             "--ignore-gitleaks-allow", "--exit-code", "0", "--log-level", "error",
             "--report-format", "json", "--report-path", str(report)],
            capture_output=True, text=True, cwd=tmp,
        )
        if proc.returncode != 0 or not report.exists():
            raise ScanInvalid(f"gitleaks did not complete (exit {proc.returncode})")
        raw = json.loads(report.read_text() or "[]")
    found: set[SecretFinding] = set()
    for f in raw:
        name = Path(f["File"]).name
        if name not in owner:
            raise ScanInvalid("gitleaks reported a file it was not given")
        length = max(0, int(f.get("EndColumn", 0)) - int(f.get("StartColumn", 0)) + 1)
        found.add(SecretFinding(owner[name], int(f.get("StartLine", 0)), str(f["RuleID"]), length))
    return sorted(found, key=lambda s: (s.index, s.line, s.rule, s.length))


# Whether gitleaks flags a name (a path, remote or ref), so it is redacted
# wherever it is shown. Filled in bulk by the main scan; any other name is
# checked when first shown. If gitleaks cannot run, the name is redacted.
NAME_HAS_SECRET: dict[str, bool] = {}


def _name_has_secret(name: str) -> bool:
    if name not in NAME_HAS_SECRET:
        try:
            NAME_HAS_SECRET[name] = bool(gitleaks_scan([name.encode("utf-8", "surrogateescape")]))
        except ScanInvalid:
            NAME_HAS_SECRET[name] = True
    return NAME_HAS_SECRET[name]


SSN_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![A-Za-z0-9_-])")
# Anchored at the start of the local part, so a long run with no "@" is
# rejected in one pass instead of being retried from every position.
EMAIL_PATTERN = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IP_PATTERN = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
ENTROPY_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9+/_=-])([A-Za-z0-9+/_=-]{41,})(?![A-Za-z0-9+/_=-])")
ENTROPY_SEGMENT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+_=-])([A-Za-z0-9+_=-]{41,})(?![A-Za-z0-9+_=-])"
)
# A branch scan cannot inspect the external object that Git LFS transfers.
# Recognize the small pointer protocol structurally below rather than assuming
# that every supported pointer will remain one byte-exact v1 spelling.
LFS_POINTER_VERSION_RE = re.compile(
    rb"version https://git-lfs\.github\.com/spec/v[0-9]+"
)
LFS_POINTER_OID_RE = re.compile(rb"oid [A-Za-z0-9][A-Za-z0-9._-]*:[0-9A-Fa-f]{32,}")
LFS_POINTER_SIZE_RE = re.compile(rb"size [0-9]+")
LFS_POINTER_EXTENSION_RE = re.compile(rb"ext-[A-Za-z0-9][A-Za-z0-9._-]* .+")

# A dotenv name: `.env`, `.envrc`, `.env.local`, `prod.env`, or a bare `env`.
# The dot before "env" is required, so a module such as Alembic's `env.py` passes.
_ENV_SEGMENT_RE = re.compile(
    r"^env$|\.env(?:rc)?(?:$|[^A-Za-z0-9])", re.IGNORECASE
)
_KEY_BASENAME_RE = re.compile(
    r"^(?:id_rsa|id_ed25519|id_dsa|id_ecdsa)(?:$|[^A-Za-z0-9])",
    re.IGNORECASE,
)
_KEY_CONTAINER_RE = re.compile(
    r"\.(?:pem|key|p12|pfx|pgp|gpg|jks|keystore|p8|ppk)(?:$|[^A-Za-z0-9])",
    re.IGNORECASE,
)
_CREDENTIAL_BASENAME_RE = re.compile(
    r"^(?:credentials\.json(?:$|[^A-Za-z0-9])|service_account(?:$|[^A-Za-z0-9]))",
    re.IGNORECASE,
)

# Shapes that never belong in a repository whose history is (or will be)
# public. Deployment-specific names come from the policy file instead.
PUBLIC_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("absolute home path", re.compile(r"/Users/[A-Za-z0-9._-]+/")),
    (
        "dated plan artifact name",
        re.compile(r"(?:[A-Z0-9]+_)*PLAN_[0-9]{4}-[0-9]{2}-[0-9]{2}\.md"),
    ),
)

SAFE_EMAIL_DOMAINS = {
    "avansaber.com",
    "anthropic.com",
    "example.com",
    "example.org",
    "example.net",
    "mystore.myshopify.com",
    # GitHub's anonymized commit identity carries no real address.
    "users.noreply.github.com",
}
SAFE_EMAIL_LOCAL_PARTS = {"noreply", "test", "dummy", "foo"}
SAFE_SHOP_EXACT = {"mystore", "example", "your-store"}
SAFE_SHOP_PREFIXES = ("test-", "demo-", "dev-")


@dataclass(frozen=True)
class KnownSafe:
    """A reviewed warning that is known to be safe: its label, the paths it may be in, and the exact shape of the
    value (both regular expressions must match the whole string). It is still reported, marked with its id."""
    id: str
    label: str
    path: re.Pattern[str]
    value: re.Pattern[str]


@dataclass(frozen=True)
class Policy:
    private_repos: frozenset[str] = frozenset()
    clean_history_repos: frozenset[str] = frozenset()
    forbidden: tuple[tuple[str, re.Pattern[str]], ...] = ()
    safe_emails: frozenset[str] = frozenset()
    known_safe: tuple[KnownSafe, ...] = ()
    source: str = "none"


POLICY = Policy()


def _load_policy(path: str | None) -> Policy:
    if not path:
        return Policy()
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ScanInvalid(f"policy file unreadable: {exc}") from exc
    try:
        forbidden = tuple(
            (str(item["label"]), re.compile(str(item["regex"]), re.IGNORECASE))
            for item in raw.get("engine_forbidden", [])
        )
    except (KeyError, TypeError, re.error) as exc:
        raise ScanInvalid(f"policy pattern invalid: {exc}") from exc
    try:
        known_safe = tuple(
            KnownSafe(str(item["id"]), str(item["label"]), re.compile(str(item["path"])), re.compile(str(item["value"])))
            for item in raw.get("known_safe", [])
            # every entry says why it is safe, in words, or the policy is invalid (a null is not a reason)
            if isinstance(item["reason"], str) and item["reason"].strip()
        )
        if len(known_safe) != len(raw.get("known_safe", [])):
            raise KeyError("reason")
    except (KeyError, TypeError, re.error) as exc:
        raise ScanInvalid(f"policy known_safe entry invalid: {exc}") from exc
    return Policy(
        private_repos=frozenset(r.lower() for r in raw.get("private_repos", [])),
        clean_history_repos=frozenset(r.lower() for r in raw.get("clean_history_repos", [])),
        forbidden=forbidden,
        safe_emails=frozenset(e.lower() for e in raw.get("safe_emails", [])),
        known_safe=known_safe,
        source=str(Path(path).resolve()),
    )


def _run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    git_env = os.environ.copy()
    for key in list(git_env):
        if (
            key in _GIT_ENVIRONMENT_BLOCKLIST
            or key in _GIT_TRANSPORT_OVERRIDE_ENV
            or key.startswith(_GIT_ENVIRONMENT_BLOCK_PREFIXES)
        ):
            git_env.pop(key, None)
    # A local refs/replace entry must never substitute cleaner bytes for the
    # commit/tree/blob objects that the push actually names. This sanitizer is
    # deliberately local to the scanner as well as the calling manager: the
    # shell entry point may be run directly from any seat.
    git_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    proc = subprocess.run(["git", *args], capture_output=True, env=git_env)
    if check and proc.returncode != 0:
        # Arguments and Git's echoed stderr can themselves contain the path or
        # URL value the scanner is trying not to disclose. The operation name
        # and status are sufficient to make an incomplete measurement fail.
        operation = args[0] if args else "<missing>"
        raise ScanInvalid(f"git {operation} failed (exit {proc.returncode})")
    return proc


def _run_network_git(
    args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """Run a network Git command with a fixed SSH transport command."""
    return _run_git([*_PINNED_GIT_NETWORK_OPTIONS, *args], check=check)


def _reject_transport_overrides() -> None:
    """Fail when a later raw push could select different Git state or transport."""
    ambient = sorted(
        key
        for key in os.environ
        if (
            key in _GIT_ENVIRONMENT_BLOCKLIST
            or key in _GIT_TRANSPORT_OVERRIDE_ENV
            or key.startswith(_GIT_ENVIRONMENT_BLOCK_PREFIXES)
        )
    )
    if ambient:
        raise ScanInvalid(
            "ambient Git repository/config/transport selector(s) are forbidden: "
            + ", ".join(ambient)
        )
    proc = _run_git(
        [
            "config",
            "--show-origin",
            "--get-regexp",
            _GIT_TRANSPORT_CONFIG_PATTERN,
        ],
        check=False,
    )
    if proc.returncode == 1 and not proc.stdout:
        return
    if proc.returncode == 0 and proc.stdout:
        count = len([line for line in proc.stdout.splitlines() if line])
        raise ScanInvalid(
            f"Git transport configuration has {count} forbidden override(s)"
        )
    raise ScanInvalid("Git transport configuration could not be measured completely")


def _decode_path(raw: bytes) -> str:
    return os.fsdecode(raw)


def _display_path(path: str) -> str:
    if _path_contains_sensitive_value(path):
        return json.dumps(
            f"<redacted-path len={len(path)}>",
            ensure_ascii=True,
        )
    return json.dumps(path, ensure_ascii=True)


def _print_invalid(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    print("SCAN INVALID. Nothing is authorized by this result.", file=sys.stderr)
    return EXIT_INVALID


def _canonical_repo(url: str) -> str | None:
    patterns = (
        r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?$",
        r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
        r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, url)
        if match:
            return match.group(1).lower()
    # Local bare mirrors are useful for an offline, end-to-end scanner test.
    # The suffix fallback is valid ONLY for an absolute local path: applying it
    # to an arbitrary host URL would let `evil.example/<owner>/<private-repo>`
    # masquerade as both supported and private.
    if os.path.isabs(url):
        normalized = url.replace("\\", "/").removesuffix(".git").lower()
        for known in POLICY.private_repos | POLICY.clean_history_repos:
            if normalized.endswith(f"/{known}"):
                return known
    return None


def _path_contains_sensitive_value(path: str) -> bool:
    if any(pattern.search(path) for _label, pattern in PII_PATTERNS):
        return True
    if SSN_PATTERN.search(path) or EMAIL_PATTERN.search(path):
        return True
    # Warning payloads must not reappear through the finding's `path=` field.
    # Redact conservatively whenever a path contains one of these shapes; the
    # warning engine still decides whether a particular match is warnable.
    if IP_PATTERN.search(path):
        return True
    if any(_looks_high_entropy(value) for value in _entropy_candidates(path)):
        return True
    return _name_has_secret(path)


def _push_urls(remote: str) -> list[str]:
    proc = _run_git(
        ["remote", "get-url", "--push", "--all", remote], check=False
    )
    if proc.returncode != 0:
        raise ScanInvalid(
            f"remote {_display_path(remote)} does not exist or has no push URL"
        )
    try:
        urls = [
            line
            for line in proc.stdout.decode("utf-8", "strict").splitlines()
            if line
        ]
    except UnicodeDecodeError as exc:
        raise ScanInvalid("push URL is not valid UTF-8 and cannot be displayed safely") from exc
    if len(urls) != 1:
        raise ScanInvalid(
            f"remote {_display_path(remote)} must have exactly one push URL; "
            f"found {len(urls)}"
        )
    _validate_push_url_for_display(urls[0])
    return urls


def _live_remote_head(remote_url: str, branch: str) -> str | None:
    proc = _run_network_git(
        [
            "ls-remote",
            "--heads",
            "--exit-code",
            remote_url,
            f"refs/heads/{branch}",
        ],
        check=False,
    )
    if proc.returncode == 2 and not proc.stdout:
        return None
    if proc.returncode != 0:
        raise ScanInvalid("live push-remote ref query failed; no scan is valid")
    records = [line for line in proc.stdout.splitlines() if line]
    if len(records) != 1:
        raise ScanInvalid(
            f"live push-remote returned {len(records)} records for branch "
            f"{_display_path(branch)}"
        )
    fields = records[0].split(b"\t", 1)
    if len(fields) != 2 or fields[1] != f"refs/heads/{branch}".encode():
        raise ScanInvalid("live push-remote returned a malformed branch record")
    try:
        oid = fields[0].decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise ScanInvalid("live push-remote returned a non-ASCII object id") from exc
    if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
        raise ScanInvalid("live push-remote returned a malformed object id")
    return oid


def _verify_binding(
    *,
    remote: str,
    initial_push_urls: list[str],
    local_branch_ref: str,
    ref_sha: str,
    live_heads: dict[str, str | None],
) -> None:
    _reject_history_overlays()
    final_push_urls = _push_urls(remote)
    if final_push_urls != initial_push_urls:
        raise ScanInvalid("push URL changed while the scan was running")
    final_sha = _resolve_commit(local_branch_ref)
    if final_sha != ref_sha:
        raise ScanInvalid(
            f"local branch moved during scan: {ref_sha} -> {final_sha}"
        )
    for branch, expected in live_heads.items():
        actual = _live_remote_head(initial_push_urls[0], branch)
        if actual != expected:
            raise ScanInvalid(
                f"live push-remote branch {_display_path(branch)} changed while "
                "the scan was running"
            )
    if _status_records():
        raise ScanInvalid("index/worktree changed while the scan was running")


def _validate_push_url_for_display(url: str) -> None:
    """Reject unsafe/leaky remote spellings before Git dereferences them.

    Git supports remote-helper/ext URL forms that can execute a local command.
    This gate deliberately accepts only the project's GitHub HTTPS/SSH/scp
    forms and absolute local paths used by offline bare-repository tests.
    """
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ScanInvalid("push URL contains control characters; URL is redacted")
    if gitleaks_scan([url.encode("utf-8", "surrogateescape")]):
        raise ScanInvalid("push URL contains a secret-shaped value; URL is redacted")
    if SSN_PATTERN.search(url):
        raise ScanInvalid("push URL contains a PII-shaped value; URL is redacted")
    if "?" in url or "#" in url:
        raise ScanInvalid("push URL contains query/fragment data and cannot be displayed safely")

    if os.path.isabs(url):
        if _path_contains_sensitive_value(url):
            raise ScanInvalid("local push URL contains a sensitive value; URL is redacted")
        return

    if url.startswith(("https://", "ssh://")):
        parsed = urlsplit(url)
        if parsed.password is not None:
            raise ScanInvalid("push URL contains embedded credentials; URL is redacted")
        if parsed.username is not None:
            if parsed.scheme != "ssh" or parsed.username != "git":
                raise ScanInvalid("push URL contains embedded userinfo; URL is redacted")
        if _canonical_repo(url) is not None:
            return
        raise ScanInvalid("unsupported non-GitHub push URL; URL is redacted")

    scp_style = re.fullmatch(r"([^@/:]+)@([^:]+):(.+)", url)
    if scp_style:
        if scp_style.group(1) != "git":
            raise ScanInvalid("push URL contains nonstandard embedded userinfo; URL is redacted")
        if _canonical_repo(url) is not None:
            return
    raise ScanInvalid(
        "unsupported push URL syntax (helpers and relative paths are forbidden); "
        "URL is redacted"
    )


def _resolve_commit(ref: str) -> str:
    proc = _run_git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], check=False)
    if proc.returncode != 0:
        raise ScanInvalid(f"ref {_display_path(ref)} does not resolve to a commit")
    value = proc.stdout.decode("ascii", "strict").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise ScanInvalid(
            f"ref {_display_path(ref)} resolved to a malformed object id"
        )
    return value


def _status_records() -> list[str]:
    raw = _run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"]).stdout
    return [_decode_path(record) for record in raw.split(b"\0") if record]


def _reject_history_overlays() -> None:
    """Refuse local mechanisms that can make a reachable parent disappear.

    ``GIT_NO_REPLACE_OBJECTS`` closes ``refs/replace`` for every Git child, but
    Git's legacy graft file and shallow boundary are separate parent-rewrite
    mechanisms.  A security scan has no useful partial verdict for either: the
    remote receives the raw commit graph, while ordinary revision walking can
    see the locally shortened graph.
    """
    shallow = _run_git(["rev-parse", "--is-shallow-repository"]).stdout
    if shallow == b"true\n":
        raise ScanInvalid(
            "repository is shallow; fetch complete history before scanning"
        )
    if shallow != b"false\n":
        raise ScanInvalid("could not determine whether repository history is shallow")

    graft_proc = _run_git(
        ["rev-parse", "--path-format=absolute", "--git-path", "info/grafts"]
    )
    if not graft_proc.stdout.endswith(b"\n"):
        raise ScanInvalid("could not resolve the legacy graft-file location")
    graft_path = os.fsdecode(graft_proc.stdout[:-1])
    # Reject even an empty file. Its presence makes the measurement dependent
    # on mutable local metadata whose only purpose is rewriting parentage.
    if os.path.lexists(graft_path):
        raise ScanInvalid(
            "legacy info/grafts exists; remove the local history overlay before scanning"
        )


def _raw_commit_parents(commit: str) -> list[str]:
    raw_commit = _run_git(["cat-file", "commit", commit]).stdout
    metadata, separator, _message = raw_commit.partition(b"\n\n")
    if not separator:
        raise ScanInvalid(f"commit object {commit} has no header/message separator")
    parents: list[str] = []
    for line in metadata.split(b"\n"):
        if not line.startswith(b"parent "):
            continue
        raw_parent = line.removeprefix(b"parent ")
        try:
            parent = raw_parent.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise ScanInvalid(f"commit object {commit} has a non-ASCII parent") from exc
        if not re.fullmatch(r"[0-9a-f]{40,64}", parent):
            raise ScanInvalid(f"commit object {commit} has a malformed parent")
        parents.append(parent)
    return parents


def _verify_introduced_commit_closure(
    commits: list[str], *, ref_sha: str, base_sha: str | None
) -> None:
    """Prove ``rev-list`` did not omit any raw ancestor introduced by the push."""
    if len(set(commits)) != len(commits):
        raise ScanInvalid("introduced commit walk contains duplicate objects")
    for commit in commits:
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ScanInvalid("introduced commit walk returned a malformed object id")

    expected_nonempty = base_sha is None or ref_sha != base_sha
    if expected_nonempty and ref_sha not in commits:
        raise ScanInvalid("introduced commit walk omitted the pushed ref tip")
    if not expected_nonempty and commits:
        raise ScanInvalid("introduced commit walk returned objects for an empty push range")

    introduced = set(commits)
    checked_boundaries: set[str] = set()
    for commit in commits:
        for parent in _raw_commit_parents(commit):
            if parent in introduced:
                continue
            if base_sha is None:
                raise ScanInvalid(
                    f"introduced commit walk omitted raw parent {parent} of {commit}"
                )
            if parent in checked_boundaries:
                continue
            ancestry = _run_git(
                ["merge-base", "--is-ancestor", parent, base_sha], check=False
            )
            if ancestry.returncode != 0:
                raise ScanInvalid(
                    f"introduced commit walk omitted raw parent {parent} of {commit}"
                )
            checked_boundaries.add(parent)


def _looks_like_lfs_pointer(data: bytes) -> bool:
    """Recognize a complete Git-LFS pointer record, not a prose quotation.

    LFS pointer records are small ASCII line protocols. Permit future spec
    versions, hash algorithms, CRLF, and extension records, but require one
    version, one object id, and one size and reject arbitrary prose lines. That
    is broader than a byte-exact v1 first-line check without turning a document
    that merely mentions the LFS URL into an external-payload finding.
    """
    if not data or len(data) > 64 * 1024 or b"\0" in data:
        return False
    try:
        data.decode("ascii", "strict")
    except UnicodeDecodeError:
        return False
    lines = data.splitlines()
    if not lines:
        return False
    version_count = 0
    oid_count = 0
    size_count = 0
    for line in lines:
        if LFS_POINTER_VERSION_RE.fullmatch(line):
            version_count += 1
        elif LFS_POINTER_OID_RE.fullmatch(line):
            oid_count += 1
        elif LFS_POINTER_SIZE_RE.fullmatch(line):
            size_count += 1
        elif LFS_POINTER_EXTENSION_RE.fullmatch(line):
            continue
        else:
            return False
    return version_count == 1 and oid_count == 1 and size_count == 1


# Warnings found in this scan, reported once per finding (same path, label and value) after the scan, however
# many commit versions carry it; hard blocks are printed at once, every time.
_WARNINGS: dict[tuple[str, str, str], dict] = {}


def _emit_redacted(
    *, commit: str, path: str, line_no: int, label: str, value: str, kind: str
) -> None:
    if kind == "WARN":
        _collect_warning(commit, path, line_no, label, value)
        return
    print(
        f"  {kind}: commit={commit[:12]} path={_display_path(path)} line={line_no} "
        f"label={label!r} value=<redacted len={len(value)}>"
    )


def _collect_warning(commit: str, path: str, line_no: int, label: str, value: str, size: int | None = None) -> None:
    """Every warning goes through here, so each is listed once and counted in the summary (review round 1)."""
    seen = _WARNINGS.setdefault((path, label, value), {"commits": [], "lines": set(), "size": None})
    if commit[:12] not in seen["commits"]:
        seen["commits"].append(commit[:12])
    seen["lines"].add(line_no)
    if size is not None:
        seen["size"] = max(size, seen["size"] or 0)


def _known_safe(path: str, label: str, value: str) -> str | None:
    for entry in POLICY.known_safe:
        if entry.label == label and entry.path.fullmatch(path) and entry.value.fullmatch(value):
            return entry.id
    return None


def _report_warnings() -> tuple[int, int]:
    """Print each unique warning once, new ones first; returns (new, known-safe) counts."""
    rows = []
    for (path, label, value), seen in _WARNINGS.items():
        rows.append((_known_safe(path, label, value), path, label, value, seen))
    rows.sort(key=lambda r: (r[0] is not None, r[0] or "", r[1], r[2], len(r[3])))
    for safe, path, label, value, seen in rows:
        lines = ",".join(str(n) for n in sorted(seen["lines"]))
        versions = f"{len(seen['commits'])} commit version{'s' if len(seen['commits']) != 1 else ''}"
        tag = f"known-safe [{safe}]" if safe else "NEW"
        what = f"value=<redacted len={len(value)}>" if value else (f"size={seen['size']}" if seen["size"] else "")
        print(f"  WARN {tag}: path={_display_path(path)} line={lines} label={label!r} "
              f"{what + ' ' if what else ''}in {versions} (first {seen['commits'][0]})")
    new = sum(1 for r in rows if r[0] is None)
    return new, len(rows) - new


def _text_views(data: bytes) -> list[str]:
    """Every text a reader could take these bytes to be.

    Latin-1 always (it decodes any byte string). When the bytes hold NULs, the
    content may be UTF-16 or UTF-32 with ASCII-range secrets spread over two or
    four bytes per character, so those decodings and a NUL-stripped view are
    scanned too."""
    views = [data.decode("latin-1")]
    if b"\x00" in data:
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            try:
                views.append(data.decode(encoding))
            except UnicodeDecodeError:
                continue
        views.append(data.replace(b"\x00", b"").decode("latin-1"))
    return views


def _iter_lines(data: bytes) -> Iterable[tuple[int, str]]:
    # Editors and Git line locators count LF boundaries. `str.splitlines()`
    # also treats vertical-tab/form-feed as line endings, which both misstates
    # the displayed line and lets `TODO<VT>secret` evade a same-line regex.
    # A line seen in an earlier view is not yielded again, so one secret is one
    # finding; line numbers refer to the view the line was found in.
    seen: set[str] = set()
    for text in _text_views(data):
        for number, line in enumerate(text.split("\n"), start=1):
            if line in seen:
                continue
            seen.add(line)
            yield number, line


_OPAQUE_MAGIC = (
    (b"\x1f\x8b", "gzip"),
    (b"PK\x03\x04", "zip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bzip2"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
)


def _opaque_kind(data: bytes) -> str | None:
    """A compressed archive hides its contents from every pattern here. ZIP is
    recognised by its central directory, so a prefixed or self-extracting
    archive is caught too."""
    for magic, kind in _OPAQUE_MAGIC:
        if data.startswith(magic):
            return kind
    if b"PK\x05\x06" in data[-65_558:] and zipfile.is_zipfile(io.BytesIO(data)):
        return "zip"
    return None


def _has_plan_trailer(commit: str) -> bool:
    """Git's own trailer parser decides what is a trailer, so a "Plan:" line in
    the body does not count."""
    proc = _run_git(
        ["log", "-1", "--format=%(trailers:key=Plan,valueonly,unfold)", commit]
    )
    values = [v.strip() for v in proc.stdout.decode("utf-8", "replace").splitlines() if v.strip()]
    return bool(values) and all(re.fullmatch(r"M[0-9]+-T[0-9]+", v) for v in values)


def _scan_hard_content(commit: str, path: str, data: bytes) -> int:
    """PII shapes and LFS pointers; secrets are gitleaks's (_gitleaks_hard_blocks)."""
    hits = 0
    if _looks_like_lfs_pointer(data):
        print(
            f"  HARD BLOCK: commit={commit[:12]} path={_display_path(path)} "
            "line=1 label='Git LFS pointer; external payload is outside this scan'"
        )
        hits += 1
    for line_no, line in _iter_lines(data):
        for label, pattern in PII_PATTERNS:
            for match in pattern.finditer(line):
                _emit_redacted(
                    commit=commit,
                    path=path,
                    line_no=line_no,
                    label=label,
                    value=match.group(0),
                    kind="HARD BLOCK",
                )
                hits += 1
        for match in SSN_PATTERN.finditer(line):
            value = match.group(0)
            _emit_redacted(
                commit=commit,
                path=path,
                line_no=line_no,
                label="US SSN (PII)",
                value=value,
                kind="HARD BLOCK",
            )
            hits += 1
    return hits


def _entropy_bits_per_char(value: str) -> float:
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_high_entropy(value: str) -> bool:
    # False-positive controls: hashes, repeated/padded placeholders, paths, and
    # single-case identifiers do not earn a warning merely for being long.
    if re.fullmatch(r"[0-9A-Fa-f]{40,}", value):
        return False
    if len(set(value)) < 12:
        return False
    if value.count("/") >= 3:
        return False
    if not (re.search(r"[a-z]", value) and re.search(r"[A-Z]", value) and re.search(r"[0-9]", value)):
        return False
    return _entropy_bits_per_char(value) >= 4.2


def _entropy_candidates(text: str) -> Iterable[str]:
    """Yield maximal tokens and slash-delimited URL/path segments once each."""
    seen: set[tuple[int, int, str]] = set()
    for pattern in (ENTROPY_TOKEN_PATTERN, ENTROPY_SEGMENT_PATTERN):
        for match in pattern.finditer(text):
            value = match.group(1)
            key = (match.start(1), match.end(1), value)
            if key in seen:
                continue
            seen.add(key)
            yield value


def _is_safe_email(value: str) -> bool:
    local, separator, domain = value.lower().rpartition("@")
    if not separator:
        return False
    if domain in SAFE_EMAIL_DOMAINS or value.lower() in POLICY.safe_emails:
        return True
    # example.com and its subdomains are IANA-reserved for documentation.
    if domain == "example.com" or domain.endswith(".example.com"):
        return True
    # These deliberately match an entire local part, not a substring such as
    # `contest`; plus-addressing is safe only for the explicit local identity.
    base_local = local.split("+", 1)[0]
    return base_local in SAFE_EMAIL_LOCAL_PARTS


def _scan_warnings(commit: str, path: str, data: bytes) -> int:
    hits = 0
    for line_no, line in _iter_lines(data):
        # Cheap mandatory delimiters prevent the permissive leading portions of
        # these regexes from retrying at every byte of a large one-line blob.
        # This is a performance guard only: each condition is necessary for the
        # corresponding regex to match, so it cannot suppress a true finding.
        if "@" in line:
            for match in EMAIL_PATTERN.finditer(line):
                value = match.group(0)
                if _is_safe_email(value):
                    continue
                _emit_redacted(
                    commit=commit, path=path, line_no=line_no,
                    label="unfamiliar email", value=value, kind="WARN",
                )
                hits += 1

        if "." in line and re.search(r"[0-9]", line):
            for match in IP_PATTERN.finditer(line):
                value = match.group(0)
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if address.is_private or address.is_loopback or address.is_unspecified or value == "255.255.255.255":
                    continue
                _emit_redacted(
                    commit=commit, path=path, line_no=line_no,
                    label="public IP address", value=value, kind="WARN",
                )
                hits += 1

        for value in _entropy_candidates(line):
            if not _looks_high_entropy(value):
                continue
            _emit_redacted(
                commit=commit, path=path, line_no=line_no,
                label="high-entropy base64-like token", value=value, kind="WARN",
            )
            hits += 1
    return hits


def _scan_regex_set(
    *, commit: str, path: str, data: bytes,
    patterns: Iterable[tuple[str, re.Pattern[str]]], kind: str,
) -> int:
    hits = 0
    for line_no, line in _iter_lines(data):
        for label, pattern in patterns:
            for match in pattern.finditer(line):
                _emit_redacted(
                    commit=commit, path=path, line_no=line_no,
                    label=label, value=match.group(0), kind=kind,
                )
                hits += 1
    return hits


def _introduced_versions(
    commits: list[str],
) -> tuple[list[tuple[str, str]], list[Version], list[ExternalPayload]]:
    all_paths: list[tuple[str, str]] = []
    versions: list[Version] = []
    external_payloads: list[ExternalPayload] = []
    seen_versions: set[tuple[str, str, str]] = set()
    seen_external: set[tuple[str, str, str]] = set()

    for commit in commits:
        path_proc = _run_git(
            [
                "diff-tree", "--root", "-m", "--no-commit-id", "-r",
                "--name-only", "-z", "--diff-filter=ACMRTUXB", commit,
            ]
        )
        for raw_path in path_proc.stdout.split(b"\0"):
            if not raw_path:
                continue
            path = _decode_path(raw_path)
            all_paths.append((commit, path))
            tree_proc = _run_git(
                ["ls-tree", "-z", commit, "--", f":(literal){path}"]
            )
            records = [record for record in tree_proc.stdout.split(b"\0") if record]
            if not records:
                raise ScanInvalid(
                    f"diff-tree path has no matching tree object at commit {commit}: "
                    f"{_display_path(path)}"
                )
            if len(records) != 1 or b"\t" not in records[0]:
                raise ScanInvalid(
                    f"could not bind commit {commit} path {_display_path(path)} to one tree object"
                )
            metadata, _ = records[0].split(b"\t", 1)
            fields = metadata.split()
            if len(fields) != 3:
                raise ScanInvalid(
                    f"malformed ls-tree metadata for commit {commit} path {_display_path(path)}"
                )
            mode, object_type, raw_oid = fields
            oid = raw_oid.decode("ascii", "strict")
            if object_type == b"commit" or mode == b"160000":
                key = (commit, path, oid)
                if key not in seen_external:
                    seen_external.add(key)
                    external_payloads.append(
                        ExternalPayload(commit=commit, path=path, oid=oid)
                    )
                continue
            if object_type != b"blob":
                raise ScanInvalid(
                    f"unexpected non-blob tree entry at commit {commit} "
                    f"path {_display_path(path)}"
                )
            key = (commit, path, oid)
            if key in seen_versions:
                continue
            seen_versions.add(key)
            data = _run_git(["cat-file", "blob", oid]).stdout
            versions.append(Version(commit=commit, path=path, oid=oid, data=data))

    return all_paths, versions, external_payloads


def _external_payload_hard_blocks(entries: Iterable[ExternalPayload]) -> int:
    hits = 0
    for entry in entries:
        print(
            f"  HARD BLOCK: commit={entry.commit[:12]} "
            f"path={_display_path(entry.path)} "
            "label='gitlink/submodule; external payload is outside this scan'"
        )
        hits += 1
    return hits


def _filename_hard_blocks(
    paths: Iterable[tuple[str, str]], *, is_private: bool
) -> int:
    """Credential-shaped names block everywhere; on a public or clean-history
    channel, internal-path and policy shapes in a name block too."""
    hits = 0
    for commit, path in paths:
        folded_path = path.casefold()
        path_segments = folded_path.split("/")
        labels: list[str] = []
        if any(_ENV_SEGMENT_RE.search(segment) for segment in path_segments):
            labels.append("disallowed .env filename")
        if any(
            _KEY_BASENAME_RE.search(segment)
            or _KEY_CONTAINER_RE.search(segment)
            or _CREDENTIAL_BASENAME_RE.search(segment)
            for segment in path_segments
        ):
            labels.append("private-key/credential filename")
        for label in labels:
            print(
                f"  HARD BLOCK: commit={commit[:12]} "
                f"path={_display_path(path)} label={label!r}"
            )
            hits += 1
        for label, pattern in PII_PATTERNS:
            for match in pattern.finditer(path):
                _emit_redacted(
                    commit=commit,
                    path=path,
                    line_no=0,
                    label=f"{label} in committed path",
                    value=match.group(0),
                    kind="HARD BLOCK",
                )
                hits += 1
        for match in SSN_PATTERN.finditer(path):
            _emit_redacted(
                commit=commit,
                path=path,
                line_no=0,
                label="US SSN (PII) in committed path",
                value=match.group(0),
                kind="HARD BLOCK",
            )
            hits += 1
        if not is_private:
            for label, pattern in PUBLIC_CONTENT_PATTERNS + POLICY.forbidden:
                for match in pattern.finditer(path):
                    print(
                        f"  HARD BLOCK: commit={commit[:12]} "
                        f"path={_display_path(path)} label={label + ' in committed path'!r} "
                        f"match_len={len(match.group(0))}"
                    )
                    hits += 1
    return hits


def _scan_main(argv: list[str]) -> int:
    global POLICY
    args = argv[1:]
    policy_path = os.environ.get("PUSH_SCAN_POLICY") or None
    if len(args) >= 2 and args[0] == "--policy":
        policy_path, args = args[1], args[2:]
    if len(args) != 2:
        return _print_invalid(
            "usage: push_scan.py [--policy <file.json>] <remote-name> <local-branch>"
        )

    remote, ref = args
    try:
        POLICY = _load_policy(policy_path)
        _WARNINGS.clear()
        repo_proc = _run_git(["rev-parse", "--show-toplevel"], check=False)
        if repo_proc.returncode != 0:
            raise ScanInvalid("current directory is not inside a Git repository")
        repo_root = Path(os.fsdecode(repo_proc.stdout.rstrip(b"\n")))
        os.chdir(repo_root)

        _reject_transport_overrides()
        _reject_history_overlays()

        dirty = _status_records()
        if dirty:
            print("Dirty index/worktree entries:", file=sys.stderr)
            for record in dirty:
                print(f"  {_display_path(record)}", file=sys.stderr)
            raise ScanInvalid("index/worktree is not clean; commit or remove every entry before scanning")

        initial_push_urls = _push_urls(remote)
        remote_url = initial_push_urls[0]
        canonical_repo = _canonical_repo(remote_url)
        if canonical_repo in POLICY.clean_history_repos:
            mode = "clean"
        elif canonical_repo in POLICY.private_repos:
            mode = "private"
        else:
            mode = "public"
        is_private = mode == "private"

        # This is a branch-push gate, not a generic revision scanner. Requiring
        # an exact local branch makes the ref that `git push <remote> <branch>`
        # would update unambiguous and excludes SHAs, tags, expressions, and
        # annotated-tag messages from this contract.
        local_branch_ref = f"refs/heads/{ref}"
        local_branch = _run_git(
            ["show-ref", "--verify", "--quiet", local_branch_ref], check=False
        )
        if local_branch.returncode != 0:
            raise ScanInvalid(
                "ref must name an exact local branch (not a SHA, tag, or revision): "
                f"{_display_path(ref)}"
            )
        ref_sha = _resolve_commit(local_branch_ref)

        base_sha: str | None = None
        base_label = "empty tree"
        live_heads: dict[str, str | None] = {}
        live_target = _live_remote_head(remote_url, ref)
        live_heads[ref] = live_target
        if live_target is not None:
            # The server advertises the exact object that the push would
            # replace. It must be present locally or the range cannot be read.
            base_sha = _resolve_commit(live_target)
            base_label = "live target branch"
            ancestry = _run_git(["merge-base", "--is-ancestor", base_sha, ref_sha], check=False)
            if ancestry.returncode != 0:
                raise ScanInvalid(
                    f"local branch {_display_path(ref)} is not a fast-forward of "
                    f"live push-remote branch {_display_path(ref)}; fetch/reconcile first"
                )
        else:
            # New branches use the live main/master baseline when available;
            # otherwise the complete branch history is scanned as a first push.
            for candidate in ("main", "master"):
                if candidate == ref:
                    continue
                live_candidate = _live_remote_head(remote_url, candidate)
                live_heads[candidate] = live_candidate
                if live_candidate is not None:
                    base_sha = _resolve_commit(live_candidate)
                    base_label = f"live default branch {_display_path(candidate)}"
                    break

        range_spec = f"{base_sha}..{ref_sha}" if base_sha else ref_sha
        commits_proc = _run_git(["rev-list", "--reverse", range_spec])
        commits = [line.decode("ascii", "strict") for line in commits_proc.stdout.splitlines() if line]
        _verify_introduced_commit_closure(
            commits, ref_sha=ref_sha, base_sha=base_sha
        )

        content_policy = PUBLIC_CONTENT_PATTERNS + POLICY.forbidden

        print(
            f"=== Push scan ({mode}): "
            f"{_display_path(remote)} ({remote_url}) ref={_display_path(ref)} ==="
        )
        print(f"Mode: {mode}")
        print(f"Repository: {canonical_repo or 'unrecognised'}")
        print(f"Policy: {POLICY.source} ({len(POLICY.private_repos)} private, "
              f"{len(POLICY.clean_history_repos)} clean-history, {len(POLICY.forbidden)} forbidden patterns)")
        print(f"Push URL: {remote_url}")
        print(f"Ref SHA: {ref_sha}")
        print(f"Live target: {live_target or 'absent'}")
        print(f"Base: {base_label} ({base_sha or 'none'})")
        print(f"Commit range: {range_spec}")
        print(f"Commits introduced: {len(commits)}")

        if not commits:
            _verify_binding(
                remote=remote,
                initial_push_urls=initial_push_urls,
                local_branch_ref=local_branch_ref,
                ref_sha=ref_sha,
                live_heads=live_heads,
            )
            print("No new commit objects to scan; the target ref may still require creation.")
            return EXIT_CLEAN

        paths, versions, external_payloads = _introduced_versions(commits)
        print(f"Committed path versions scanned: {len(versions)}")
        print(f"Committed path records inspected: {len(paths)}")
        print(f"External payload entries found: {len(external_payloads)}")

        messages: list[tuple[str, bytes, bytes]] = []
        for commit in commits:
            raw_commit = _run_git(["cat-file", "commit", commit]).stdout
            metadata, separator, message = raw_commit.partition(b"\n\n")
            if not separator:
                raise ScanInvalid(f"commit object {commit} has no header/message separator")
            messages.append((commit, metadata, message))
        # One gitleaks run over everything the push carries. A target is
        # (commit, path, kind): kind "name" (the path itself), "blob" (a path
        # version's bytes) or "meta" (a commit's metadata or message; its
        # path is a fixed label, never a real path).
        targets: list[tuple[str, str, str]] = []
        items: list[bytes] = []
        for commit, path in paths:
            targets.append((commit, path, "name"))
            items.append(os.fsencode(path))
        for version in versions:
            targets.append((version.commit, version.path, "blob"))
            items.append(version.data)
        for commit, metadata, message in messages:
            targets += [(commit, "<commit-metadata>", "meta"), (commit, "<commit-message>", "meta")]
            items += [metadata, message]
        secrets = gitleaks_scan(items)
        flagged_names = {targets[s.index][1] for s in secrets if targets[s.index][2] == "name"}
        for commit, path in paths:
            NAME_HAS_SECRET[path] = path in flagged_names

        print("\n[HARD-BLOCK checks]")
        for s in secrets:
            commit, path, kind = targets[s.index]
            shown = json.dumps(path) if kind == "meta" else _display_path(path)
            where = "line=0" if kind == "name" else f"line={s.line}"
            print(f"  HARD BLOCK: commit={commit[:12]} path={shown} {where} "
                  f"label='secret ({s.rule}){' in committed path' if kind == 'name' else ''}' value=<redacted len={s.length}>")
        hard_hits = len(secrets)
        hard_hits += _filename_hard_blocks(paths, is_private=is_private)
        hard_hits += _external_payload_hard_blocks(external_payloads)
        warn_hits = 0
        for version in versions:
            hard_hits += _scan_hard_content(
                version.commit, version.path, version.data
            )
            opaque = _opaque_kind(version.data)
            if opaque:
                label = f"opaque {opaque} payload; its contents are outside this scan"
                if is_private:
                    _collect_warning(version.commit, version.path, 0, label, "")
                    warn_hits += 1
                else:
                    print(f"  HARD BLOCK: commit={version.commit[:12]} path={_display_path(version.path)} "
                          f"label={label!r}")
                    hard_hits += 1
            if not is_private:
                hard_hits += _scan_regex_set(
                    commit=version.commit,
                    path=version.path,
                    data=version.data,
                    patterns=content_policy,
                    kind="HARD BLOCK",
                )

        print("\n[Commit-message checks]")
        for commit, metadata, message in messages:
            hard_hits += _scan_hard_content(
                commit, "<commit-metadata>", metadata
            )
            warn_hits += _scan_warnings(
                commit, "<commit-metadata>", metadata
            )
            hard_hits += _scan_hard_content(commit, "<commit-message>", message)
            if not is_private:
                hard_hits += _scan_regex_set(
                    commit=commit,
                    path="<commit-message>",
                    data=message,
                    patterns=content_policy,
                    kind="HARD BLOCK",
                )
                if not _has_plan_trailer(commit):
                    print(
                        f"  HARD BLOCK: commit={commit[:12]} path=\"<commit-message>\" "
                        "label='no Plan trailer'"
                    )
                    hard_hits += 1
            warn_hits += _scan_warnings(commit, "<commit-message>", message)

        print("\n[WARN-ONLY checks]")
        for commit, path in paths:
            # A filename is push-visible data too. Feeding its original bytes
            # through the same warn engine catches unfamiliar email/domain/IP,
            # TODO, and entropy values; _display_path redacts a sensitive path.
            warn_hits += _scan_warnings(commit, path, os.fsencode(path))
        for version in versions:
            warn_hits += _scan_warnings(
                version.commit, version.path, version.data
            )
            if len(version.data) > 500_000:
                _collect_warning(version.commit, version.path, 0, "file over 500KB", "", size=len(version.data))
                warn_hits += 1

        new_warnings, safe_warnings = _report_warnings()

        _verify_binding(
            remote=remote,
            initial_push_urls=initial_push_urls,
            local_branch_ref=local_branch_ref,
            ref_sha=ref_sha,
            live_heads=live_heads,
        )
        _reject_transport_overrides()

        print("\n=== Scan result ===")
        print(f"Ref SHA:          {ref_sha}")
        print(f"Commits:          {len(commits)}")
        print(f"Path versions:    {len(versions)}")
        print(f"External entries: {len(external_payloads)}")
        print(f"Hard-block hits:  {hard_hits}")
        print(f"Warning hits:     {warn_hits}")
        print(f"Unique warnings:  {new_warnings + safe_warnings} ({new_warnings} new, {safe_warnings} known-safe)")

        if hard_hits:
            print("PUSH BLOCKED. Remove every hard-block match and rewrite affected history before rescanning.")
            return EXIT_HARD_BLOCK
        channel = {
            "private": "Private channel: after a second complete, agreeing scan of this "
                       "exact SHA/range, push without another approval.",
            "clean": "Clean-history private channel: after a second complete, agreeing "
                     "scan of this exact SHA/range, push without another approval.",
            "public": "Public channel: this result never authorizes a push on its own; "
                      "the repository owner's explicit approval for this one push is required.",
        }[mode]
        if warn_hits:
            print(f"WARNINGS ONLY ({mode}). Read every warning above.")
        else:
            print(f"CLEAN ({mode}). Scan found no hard blocks and no warnings.")
        print(channel)
        print(f"Push exactly ref SHA {ref_sha}; a moved branch expires this result.")
        return EXIT_WARN if warn_hits else EXIT_CLEAN
    except (ScanInvalid, UnicodeError, OSError) as exc:
        return _print_invalid(str(exc))


def main(argv: list[str]) -> int:
    return _scan_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
