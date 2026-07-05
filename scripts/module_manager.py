#!/usr/bin/env python3
"""ERPClaw Module Manager — install, update, and manage GitHub-hosted modules.

Handles discovery, installation, dependency resolution, and action cache
management for ERPClaw expansion modules. Modules are git-cloned into
~/.openclaw/erpclaw/modules/{module-name}/ and tracked in the
erpclaw_module / erpclaw_module_action tables.

Usage: python3 module_manager.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

# ---------------------------------------------------------------------------
# Shared library imports
# ---------------------------------------------------------------------------
# Bootstrap the lib onto sys.path via the ERPCLAW_HOME point of truth
# (ADR-0017). This is the chicken-and-egg site: it makes erpclaw_lib importable,
# so it resolves the lib dir inline (equivalent to erpclaw_lib.paths.lib_dir()).
# With ERPCLAW_HOME unset this equals os.path.expanduser("~/.openclaw/erpclaw/lib").
sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
from erpclaw_lib.db import get_connection
from erpclaw_lib.paths import db_default, install_state_dir, lib_dir, modules_dir
from erpclaw_lib.response import ok, err, rows_to_list, row_to_dict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODULES_DIR = modules_dir()

# OpenClaw's agent / `openclaw skills list` only discovers skills under
# `~/.openclaw/workspace/skills/`. Modules installed by this manager
# also need to be published there so the agent can invoke their actions
# (otherwise the agent reports "no integration set up" even though the
# module is installed and working from the CLI). Symlinks are rejected
# by the openclaw skills subsystem with `reason=symlink-escape`, so we
# do a plain copy. Confirmed empirically 2026-04-25 against the live
# agent on the OpenClaw Ubuntu server.
OPENCLAW_WORKSPACE_SKILLS_DIR = os.path.expanduser(
    "~/.openclaw/workspace/skills"
)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(SCRIPT_DIR, "module_registry.json")
REMOTE_REGISTRY_URL = "https://raw.githubusercontent.com/avansaber/erpclaw/main/scripts/module_registry.json"
LOCAL_CACHE_PATH = os.path.join(install_state_dir(), "registry_cache.json")
CACHE_TTL_SECONDS = 86400  # 24 hours

# Foundation source synchronization. Install-state markers resolve through the
# ERPCLAW_HOME point of truth (ADR-0017); ERPCLAW_HOME unset ⇒ today's
# ~/.openclaw/erpclaw/* paths byte-for-byte.
FOUNDATION_INSTALL_ROOT = os.path.dirname(SCRIPT_DIR)  # parent of scripts/
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/avansaber/erpclaw/main"
SYNC_LOCK_PATH = os.path.join(install_state_dir(), ".sync.lock")
LAST_SYNC_MARKER = os.path.join(install_state_dir(), ".last_sync")
NO_AUTOSYNC_MARKER = os.path.join(install_state_dir(), ".no_autosync")
SYNC_LOG_PATH = os.path.join(install_state_dir(), "logs", "sync.log")

# Skip filters (must match install_module's full-tree verification block).
# `.clawhub` is the ClawHub CLI's per-skill metadata directory (CLI v0.12.3
# installs by FILE EXTRACTION into the skill dir, then writes its own tracking
# metadata under `.clawhub/` and a top-level `_meta.json`). Neither is part of
# the ed25519-signed foundation manifest, so the reconcile walk must exclude
# them — otherwise `update-foundation`'s orphan-cleanup (ADR-0028: files +
# migrations converge) would flag CLI-owned metadata as "orphaned" and delete
# it, corrupting ClawHub's own upgrade tracking on the next `clawhub update`.
SYNC_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", "dist", "build", ".clawhub"}
SYNC_SKIP_SUFFIXES = (".pyc", ".pyo", ".bak", ".tmp", ".DS_Store")
SYNC_SKIP_RELPATHS_FOUNDATION = {
    "scripts/module_registry.json",
    "scripts/module_registry.json.sig",
    "scripts/signing_log.txt",
}
# ClawHub-owned metadata filenames excluded at ANY depth (basename match),
# mirroring erpclaw_lib.skip_filters.SKIP_FILE_EXACT's handling of the sibling
# `.clawhubignore`. See the SYNC_SKIP_DIRS note above for the ADR-0028 rationale.
SYNC_SKIP_BASENAMES_FOUNDATION = {"_meta.json"}


def _now_iso():
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


REMOTE_SIGNATURE_URL = REMOTE_REGISTRY_URL + ".sig"
LOCAL_SIG_CACHE_PATH = LOCAL_CACHE_PATH + ".sig"
LOCAL_VERSION_TRACKER = os.path.join(install_state_dir(), ".last_registry_version")


class _RegistrySignatureError(Exception):
    """Raised when registry signature verification fails (strict mode)."""


def _verify_registry_payload(raw_bytes, sig_hex, *, label):
    """Run ed25519 verification + monotonic-version check on a registry payload.

    Returns the parsed registry dict on success. Raises _RegistrySignatureError
    on signature failure or downgrade attempt.
    """
    # Late import so non-foundation paths (e.g., scripts that import
    # module_manager for testing) don't require the lib to be installed.
    sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    try:
        from erpclaw_lib.signing import (
            verify_registry_signature, REGISTRY_VERSION_FIELD, fingerprint,
        )
        from cryptography.exceptions import InvalidSignature
    except ImportError as e:
        raise _RegistrySignatureError(f"signing library unavailable: {e}")

    try:
        trusted = verify_registry_signature(raw_bytes, sig_hex)
    except InvalidSignature as e:
        raise _RegistrySignatureError(f"signature verification failed ({label}): {e}")

    try:
        registry = json.loads(raw_bytes)
    except json.JSONDecodeError as e:
        raise _RegistrySignatureError(f"registry not valid JSON ({label}): {e}")

    incoming = int(registry.get(REGISTRY_VERSION_FIELD, 0) or 0)
    local_last = 0
    if os.path.isfile(LOCAL_VERSION_TRACKER):
        try:
            local_last = int((open(LOCAL_VERSION_TRACKER).read() or "0").strip() or "0")
        except (ValueError, OSError):
            local_last = 0
    if incoming < local_last:
        raise _RegistrySignatureError(
            f"registry_version downgrade refused ({label}): "
            f"incoming={incoming}, local_last={local_last}"
        )
    if incoming > local_last:
        try:
            os.makedirs(os.path.dirname(LOCAL_VERSION_TRACKER), exist_ok=True)
            with open(LOCAL_VERSION_TRACKER, "w") as f:
                f.write(str(incoming))
        except OSError:
            pass

    registry["_signed_by"] = fingerprint(trusted.public_key_hex)
    return registry


def _fetch_with_retry(url, *, timeout=10, retries=1, retry_delay=5.0):
    """Fetch a URL with one retry-after-delay. Defends against CDN propagation lag."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "erpclaw"})
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.read()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(retry_delay)
                continue
    raise last_err


def _bundled_foundation_version():
    """Return the foundation version recorded in the bundled (just-installed)
    registry, or None if unavailable. Used to detect a fresh foundation upgrade
    so we can invalidate the data-dir cache that holds the OLD foundation's
    manifest (T1.2 in PENDING_WORK_PLAN_2026-05-10.md).
    """
    bundled = os.path.join(SCRIPT_DIR, "module_registry.json")
    try:
        with open(bundled, "rb") as f:
            data = json.loads(f.read())
        modules = data.get("modules") or {}
        if isinstance(modules, dict):
            return (modules.get("erpclaw") or {}).get("version")
        for m in modules:
            if m.get("name") == "erpclaw":
                return m.get("version")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return None


def _cached_foundation_version():
    """Return the foundation version recorded in the cached registry, or None."""
    try:
        with open(LOCAL_CACHE_PATH, "rb") as f:
            data = json.loads(f.read())
        modules = data.get("modules") or {}
        if isinstance(modules, dict):
            return (modules.get("erpclaw") or {}).get("version")
        for m in modules:
            if m.get("name") == "erpclaw":
                return m.get("version")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return None


def _cache_is_behind_bundled():
    """True if the data-dir cache holds an older foundation version than the
    just-installed bundled registry. When True, the cache should be invalidated
    before any module install attempt — otherwise the install-time integrity
    check verifies foundation files against the OLD manifest and reports
    false-positive mismatches for any file that changed between versions.
    """
    if not os.path.isfile(LOCAL_CACHE_PATH):
        return False
    bundled = _bundled_foundation_version()
    cached = _cached_foundation_version()
    if not bundled or not cached:
        return False
    # Simple semver compare via tuple of ints; works for our X.Y.Z scheme.
    def _to_tuple(v):
        try:
            return tuple(int(p) for p in str(v).split("."))
        except ValueError:
            return (0,)
    return _to_tuple(bundled) > _to_tuple(cached)


def _load_registry(force_refresh=False):
    """Load module registry. Lenient mode: signature is verified and reported
    via `_signed_by`/`_signature_warning`, but the function does not refuse on
    failure. Used by read-only listings (`available-modules`, etc.).

    For foundation reconciliation, callers MUST use `_load_registry_strict`
    which refuses unsigned/tampered/downgraded registries.

    Resolution order: fresh cache → remote → bundled → stale cache.

    Auto-invalidation: when the bundled foundation version is newer than the
    cached one (i.e., the foundation skill was just upgraded via
    `clawhub install`), the cache is treated as stale regardless of mtime —
    its hashes describe the OLD foundation and using them would produce
    false-positive integrity warnings.
    """
    bundled_path = os.path.join(SCRIPT_DIR, "module_registry.json")
    bundled_sig_path = bundled_path + ".sig"

    if _cache_is_behind_bundled():
        force_refresh = True

    # 1. Check local cache
    if not force_refresh and os.path.isfile(LOCAL_CACHE_PATH):
        try:
            age = time.time() - os.path.getmtime(LOCAL_CACHE_PATH)
            if age < CACHE_TTL_SECONDS:
                raw = open(LOCAL_CACHE_PATH, "rb").read()
                sig = ""
                if os.path.isfile(LOCAL_SIG_CACHE_PATH):
                    sig = open(LOCAL_SIG_CACHE_PATH).read().strip()
                try:
                    return _verify_registry_payload(raw, sig, label="local-cache")
                except _RegistrySignatureError as e:
                    data = json.loads(raw)
                    data["_signature_warning"] = str(e)
                    return data
        except (json.JSONDecodeError, OSError):
            pass

    # 2. Try remote fetch (registry + signature)
    try:
        raw = _fetch_with_retry(REMOTE_REGISTRY_URL, retries=1)
        sig = ""
        try:
            sig_bytes = _fetch_with_retry(REMOTE_SIGNATURE_URL, retries=1)
            sig = sig_bytes.decode("utf-8").strip()
        except Exception as e:
            # Empty sig → downstream verify_registry_payload will fail and
            # downgrade gracefully to an unsigned registry with _signature_warning.
            # Logging the cause lets operators distinguish network flake from
            # an intentionally-missing signature file.
            print(f"WARN: signature fetch failed for {REMOTE_SIGNATURE_URL}: {e}", file=sys.stderr)
        try:
            data = _verify_registry_payload(raw, sig, label="remote")
        except _RegistrySignatureError as e:
            data = json.loads(raw)
            data["_signature_warning"] = str(e)
        # Update cache
        cache_dir = os.path.dirname(LOCAL_CACHE_PATH)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        with open(LOCAL_CACHE_PATH, "wb") as f:
            f.write(raw)
        if sig:
            with open(LOCAL_SIG_CACHE_PATH, "w") as f:
                f.write(sig)
        return data
    except Exception:
        pass  # Offline or error — fall through

    # 3. Fall back to bundled copy + bundled signature
    if os.path.isfile(bundled_path):
        try:
            raw = open(bundled_path, "rb").read()
            sig = ""
            if os.path.isfile(bundled_sig_path):
                sig = open(bundled_sig_path).read().strip()
            try:
                return _verify_registry_payload(raw, sig, label="bundled")
            except _RegistrySignatureError as e:
                data = json.loads(raw)
                data["_signature_warning"] = str(e)
                return data
        except (json.JSONDecodeError, OSError):
            pass

    # 4. Fall back to stale cache
    if os.path.isfile(LOCAL_CACHE_PATH):
        try:
            raw = open(LOCAL_CACHE_PATH, "rb").read()
            sig = ""
            if os.path.isfile(LOCAL_SIG_CACHE_PATH):
                sig = open(LOCAL_SIG_CACHE_PATH).read().strip()
            try:
                return _verify_registry_payload(raw, sig, label="stale-cache")
            except _RegistrySignatureError as e:
                data = json.loads(raw)
                data["_signature_warning"] = str(e)
                return data
        except (json.JSONDecodeError, OSError):
            pass

    return {"version": "0.0.0", "modules": {}}


def _load_registry_strict(force_refresh=True):
    """Strict mode: fetch + signature-verify + monotonic-check. Refuses unsigned.

    Used by `update_foundation_action` for the trust-root path. Returns a
    verified registry dict; raises `_RegistrySignatureError` on any failure
    (no fallback to unsigned bundled / stale cache).
    """
    # Try remote first (force_refresh by default for strict)
    last_err = None
    if force_refresh:
        try:
            raw = _fetch_with_retry(REMOTE_REGISTRY_URL, retries=1)
            sig_bytes = _fetch_with_retry(REMOTE_SIGNATURE_URL, retries=1)
            sig = sig_bytes.decode("utf-8").strip()
            data = _verify_registry_payload(raw, sig, label="remote-strict")
            # Cache the verified content
            try:
                cache_dir = os.path.dirname(LOCAL_CACHE_PATH)
                os.makedirs(cache_dir, exist_ok=True)
                with open(LOCAL_CACHE_PATH, "wb") as f:
                    f.write(raw)
                with open(LOCAL_SIG_CACHE_PATH, "w") as f:
                    f.write(sig)
            except OSError:
                pass
            return data
        except _RegistrySignatureError as e:
            raise
        except Exception as e:
            last_err = e
            # Network failure → fall through to cache, but only if cache is
            # itself signed and fresh. Bundled fallback is allowed because
            # bundled .sig is also checked.

    # Fall back to bundled (always signature-verified in strict mode)
    bundled_path = os.path.join(SCRIPT_DIR, "module_registry.json")
    bundled_sig_path = bundled_path + ".sig"
    if os.path.isfile(bundled_path) and os.path.isfile(bundled_sig_path):
        raw = open(bundled_path, "rb").read()
        sig = open(bundled_sig_path).read().strip()
        return _verify_registry_payload(raw, sig, label="bundled-strict")

    # Last resort: cached (signed only)
    if os.path.isfile(LOCAL_CACHE_PATH) and os.path.isfile(LOCAL_SIG_CACHE_PATH):
        raw = open(LOCAL_CACHE_PATH, "rb").read()
        sig = open(LOCAL_SIG_CACHE_PATH).read().strip()
        return _verify_registry_payload(raw, sig, label="cached-strict")

    raise _RegistrySignatureError(
        f"strict load: no signed registry available "
        f"(remote: {last_err}; bundled missing or unsigned; cache missing or unsigned)"
    )


def _registry_to_dict(registry):
    """Convert registry modules (dict-keyed or list) to {name: info} dict."""
    modules_raw = registry.get("modules", {})
    if isinstance(modules_raw, dict):
        result = {}
        for name, info in modules_raw.items():
            info_copy = dict(info)
            info_copy.setdefault("name", name)
            result[name] = info_copy
        return result
    # List format (fallback)
    return {m["name"]: m for m in modules_raw}


def _get_installed_modules(conn):
    """Return dict of installed module names -> row dicts."""
    rows = conn.execute("SELECT * FROM erpclaw_module").fetchall()
    return {row["name"]: dict(row) for row in rows}


def _get_git_commit(install_path):
    """Get the current git commit hash for a module directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=install_path, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _check_remote_updates(install_path):
    """Check if the module has updates available on origin/main.

    Returns (has_updates: bool, local_commit: str, remote_commit: str).
    """
    local_commit = _get_git_commit(install_path)
    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=install_path, capture_output=True, text=True, timeout=30
        )
        result = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=install_path, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            remote_commit = result.stdout.strip()
            return (local_commit != remote_commit, local_commit, remote_commit)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return (False, local_commit, None)


# ---------------------------------------------------------------------------
# Action cache builder — uses AST parsing (safe, no side effects)
# ---------------------------------------------------------------------------

def _extract_actions_via_ast(script_path):
    """Extract action names from a module's db_query.py using AST parsing.

    Looks for top-level assignments to ACTIONS, ACTION_MAP, and ALIASES dicts.
    Extracts the string keys from each. This is safer than importing the module
    since it avoids executing any code or triggering import side effects.
    """
    if not os.path.isfile(script_path):
        return set()

    try:
        with open(script_path, "r") as f:
            source = f.read()
        tree = ast.parse(source, filename=script_path)
    except (SyntaxError, OSError):
        return set()

    target_names = {"ACTIONS", "ACTION_MAP", "ALIASES"}
    all_actions = set()

    for node in ast.iter_child_nodes(tree):
        # Match: ACTIONS = { ... }
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in target_names:
                    all_actions |= _extract_dict_keys(node.value)
        # Match: ACTIONS.update({ ... }) — Pattern B merge from domain modules
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (isinstance(call.func, ast.Attribute)
                    and call.func.attr == "update"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in target_names
                    and call.args):
                all_actions |= _extract_dict_keys(call.args[0])

    # Remove 'status' to avoid collision — each module has its own status
    all_actions.discard("status")
    return all_actions


def _extract_dict_keys(node):
    """Extract string keys from a Dict AST node."""
    keys = set()
    if isinstance(node, ast.Dict):
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


def _extract_actions_via_regex(script_path):
    """Fallback: extract action names using regex on the source text.

    Matches patterns like:
        "action-name": some_function,
        'action-name': some_function,
    within ACTIONS = { ... } blocks.
    """
    if not os.path.isfile(script_path):
        return set()

    try:
        with open(script_path, "r") as f:
            source = f.read()
    except OSError:
        return set()

    actions = set()
    # Find ACTIONS = { ... } block (and ACTION_MAP, ALIASES)
    for var_name in ("ACTIONS", "ACTION_MAP", "ALIASES"):
        pattern = rf'{var_name}\s*=\s*\{{([^}}]*)\}}'
        match = re.search(pattern, source, re.DOTALL)
        if match:
            block = match.group(1)
            # Extract quoted string keys
            actions |= set(re.findall(r'["\']([a-z][a-z0-9\-]+)["\']', block))
        # Also match .update({...})
        pattern_update = rf'{var_name}\.update\(\s*\{{([^}}]*)\}}\s*\)'
        for m in re.finditer(pattern_update, source, re.DOTALL):
            block = m.group(1)
            actions |= set(re.findall(r'["\']([a-z][a-z0-9\-]+)["\']', block))

    actions.discard("status")
    return actions


def build_action_cache(conn, module_name, install_path):
    """Scan a module's db_query.py and cache its action names.

    Uses AST parsing as the primary method, falling back to regex if AST
    yields no results (e.g., dynamically constructed dicts).
    Also scans sibling .py files in scripts/ for domain modules that
    define their own ACTIONS dicts (merged via ACTIONS.update()).

    Returns the number of actions cached.
    """
    script_path = os.path.join(install_path, "scripts", "db_query.py")

    all_actions = _extract_actions_via_ast(script_path)
    if not all_actions:
        all_actions = _extract_actions_via_regex(script_path)

    # Also scan sibling domain modules in scripts/ directory
    scripts_dir = os.path.join(install_path, "scripts")
    if os.path.isdir(scripts_dir):
        for fname in os.listdir(scripts_dir):
            if fname.endswith(".py") and fname != "db_query.py":
                domain_path = os.path.join(scripts_dir, fname)
                domain_actions = _extract_actions_via_ast(domain_path)
                if not domain_actions:
                    domain_actions = _extract_actions_via_regex(domain_path)
                all_actions |= domain_actions

    if not all_actions:
        return 0

    # Clear existing cache for this module and insert fresh
    conn.execute(
        "DELETE FROM erpclaw_module_action WHERE module_name = ?",
        (module_name,)
    )
    conn.executemany(
        "INSERT OR REPLACE INTO erpclaw_module_action (module_name, action_name) VALUES (?, ?)",
        [(module_name, a) for a in sorted(all_actions)]
    )
    conn.commit()

    # Check for action name collisions with other modules (non-fatal warning)
    collisions = conn.execute(
        """SELECT action_name, module_name FROM erpclaw_module_action
           WHERE action_name IN ({}) AND module_name != ?""".format(
            ",".join("?" for _ in all_actions)),
        list(all_actions) + [module_name]
    ).fetchall()
    if collisions:
        import sys as _sys
        for c in collisions:
            _sys.stderr.write(
                f"[module-manager] WARNING: action '{c['action_name']}' in '{module_name}' "
                f"collides with '{c['module_name']}'\n"
            )

    return len(all_actions)


# ---------------------------------------------------------------------------
# SKILL.md regeneration — appends installed module actions to deployed SKILL.md
# ---------------------------------------------------------------------------

def _regenerate_skill_md(conn):
    """Regenerate the deployed SKILL.md with installed module actions appendix.

    Reads the source SKILL.md as a template, appends an auto-generated section
    listing all installed module actions, and writes to the deployed location.
    The source template is the installed skill's SKILL.md (without the appendix).
    """
    # Find the deployed SKILL.md path
    deployed_path = os.path.expanduser("~/clawd/skills/erpclaw/SKILL.md")
    # Source template is in the same skill directory
    source_path = os.path.join(SCRIPT_DIR, "..", "SKILL.md")

    # Use source if it exists, otherwise use deployed as template
    template_path = source_path if os.path.isfile(source_path) else deployed_path
    if not os.path.isfile(template_path):
        return  # No SKILL.md to regenerate

    try:
        with open(template_path, "r") as f:
            content = f.read()
    except OSError:
        return

    # Strip any existing auto-generated appendix
    marker = "## Installed Module Actions"
    if marker in content:
        content = content[:content.index(marker)].rstrip() + "\n"

    # Query installed modules and their actions
    rows = conn.execute(
        """SELECT ma.module_name, ma.action_name, m.display_name, m.action_count
           FROM erpclaw_module_action ma
           JOIN erpclaw_module m ON m.name = ma.module_name
           WHERE m.install_status = 'installed' AND m.is_active = 1
           ORDER BY ma.module_name, ma.action_name"""
    ).fetchall()

    if not rows:
        # No modules installed — write template without appendix
        if os.path.isfile(deployed_path):
            try:
                with open(deployed_path, "w") as f:
                    f.write(content)
            except OSError:
                pass
        return

    # Group actions by module
    module_actions = {}
    module_display = {}
    module_counts = {}
    for r in rows:
        mod = r["module_name"]
        module_actions.setdefault(mod, []).append(r["action_name"])
        module_display[mod] = r["display_name"]
        module_counts[mod] = r["action_count"] or len(module_actions[mod])

    # Read module descriptions from SKILL.md files for context
    def _get_module_desc(module_name):
        """Read first line of description from module's SKILL.md."""
        for base in [os.path.join(MODULES_DIR, module_name),
                     os.path.join(SCRIPT_DIR, "..", "..", module_name)]:
            skill_path = os.path.join(base, "SKILL.md")
            if os.path.isfile(skill_path):
                try:
                    with open(skill_path, "r") as f:
                        for line in f:
                            if line.startswith("description:"):
                                desc = line.split(":", 1)[1].strip().strip(">").strip()
                                if desc:
                                    return desc[:120]
                except OSError:
                    pass
        return ""

    # Build appendix
    appendix = f"\n\n{marker}\n"
    appendix += "<!-- AUTO-GENERATED — do not edit manually. Regenerated on module install/uninstall. -->\n\n"

    for mod in sorted(module_actions.keys()):
        actions = module_actions[mod]
        display = module_display.get(mod, mod)
        count = len(actions)
        desc = _get_module_desc(mod)

        appendix += f"### {display} ({count} actions)\n"
        if desc:
            appendix += f"{desc}\n"

        # Show key actions (up to 10)
        key_actions = actions[:10]
        appendix += f"Key actions: {', '.join(f'`{a}`' for a in key_actions)}"
        if len(actions) > 10:
            appendix += f", ... (+{len(actions) - 10} more)"
        appendix += "\n\n"

    # Write to deployed path
    deployed_dir = os.path.dirname(deployed_path)
    if os.path.isdir(deployed_dir):
        try:
            with open(deployed_path, "w") as f:
                f.write(content + appendix)
        except OSError:
            pass  # Non-fatal — skill still works, just without action discovery


# ---------------------------------------------------------------------------
# Action: install-module
# ---------------------------------------------------------------------------

def install_module(args):
    """Install a module from GitHub.

    Resolves dependencies first (auto-installing missing ones), clones the
    repo, runs init_db.py if present, reads module.json for metadata, builds
    the action cache, and registers in erpclaw_module.
    """
    module_name = args.module_name
    if not module_name:
        err("--module-name is required")

    registry = _load_registry()
    modules_by_name = _registry_to_dict(registry)

    if module_name not in modules_by_name:
        err(
            f"Module '{module_name}' not found in registry",
            suggestion="Run --action available-modules to see all available modules"
        )

    module_info = modules_by_name[module_name]
    conn = get_connection()

    # Check if already installed
    existing = conn.execute(
        "SELECT id, version, install_status FROM erpclaw_module WHERE name = ?",
        (module_name,)
    ).fetchone()
    if existing:
        if existing["install_status"] == "installed":
            err(
                f"Module '{module_name}' is already installed (version {existing['version']})",
                suggestion="Use --action update-modules to update, or --action remove-module to reinstall"
            )
        else:
            # Previous install failed — clean up and retry
            _cleanup_failed_install(conn, module_name)

    # Resolve dependencies
    requires = module_info.get("requires", [])
    if requires:
        installed = _get_installed_modules(conn)
        missing = [r for r in requires if r not in installed]
        if missing:
            auto_installed = []
            for dep in missing:
                if dep not in modules_by_name:
                    err(
                        f"Dependency '{dep}' for module '{module_name}' not found in registry",
                        suggestion="This module has an unresolvable dependency"
                    )
                # Recursively install dependency
                dep_args = argparse.Namespace(module_name=dep)
                try:
                    # Temporarily suppress ok() exit to allow chaining
                    _install_module_inner(dep_args, conn, modules_by_name, depth=1)
                    auto_installed.append(dep)
                except SystemExit:
                    # ok() calls sys.exit(0) — we need to re-open connection
                    conn = get_connection()
                    auto_installed.append(dep)

    # Perform the actual installation
    result = _install_module_inner(args, conn, modules_by_name, depth=0)
    ok(result)


def _install_module_inner(args, conn, modules_by_name, depth=0):
    """Inner installation logic. Returns result dict instead of calling ok().

    The depth parameter tracks recursive dependency installs to prevent
    infinite loops.
    """
    if depth > 10:
        err("Dependency resolution exceeded maximum depth (10) — circular dependency detected")

    module_name = args.module_name
    module_info = modules_by_name[module_name]

    # Check again (may have been installed as a dependency in this session)
    existing = conn.execute(
        "SELECT id FROM erpclaw_module WHERE name = ? AND install_status = 'installed'",
        (module_name,)
    ).fetchone()
    if existing:
        return {"module": module_name, "note": "already installed (as dependency)"}

    install_path = os.path.join(MODULES_DIR, module_name)
    github_repo = module_info.get("github", module_info.get("github_repo", ""))
    subdir = module_info.get("subdir", "")
    now = _now_iso()
    module_id = str(uuid4())

    # Mark as installing
    conn.execute(
        """INSERT INTO erpclaw_module
           (id, name, display_name, version, category, github_repo,
            install_path, installed_at, updated_at, install_status, requires_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'updating', ?)""",
        (module_id, module_name, module_info.get("display_name", module_name),
         module_info.get("version", "0.0.0"), module_info.get("category", "expansion"),
         github_repo, install_path, now, now,
         json.dumps(module_info.get("requires", [])))
    )
    conn.commit()

    # Clone the repository
    os.makedirs(MODULES_DIR, exist_ok=True)
    if os.path.isdir(install_path):
        shutil.rmtree(install_path)

    clone_url = f"https://github.com/{github_repo}.git"

    if subdir:
        # Grouped repo — use sparse checkout to get only the needed subdir
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix=f"erpclaw-install-{module_name}-")
        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", "--filter=blob:none",
                 "--sparse", clone_url, tmp_dir],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                _mark_failed(conn, module_name, f"git clone failed: {result.stderr.strip()}")
                err(f"Failed to clone {clone_url}: {result.stderr.strip()}")

            result = subprocess.run(
                ["git", "-C", tmp_dir, "sparse-checkout", "set", subdir],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                _mark_failed(conn, module_name, f"sparse-checkout failed: {result.stderr.strip()}")
                err(f"Failed sparse-checkout for {subdir}: {result.stderr.strip()}")

            # Move the subdir to the install path
            subdir_path = os.path.join(tmp_dir, subdir)
            if not os.path.isdir(subdir_path):
                _mark_failed(conn, module_name, f"subdir '{subdir}' not found in repo")
                err(f"Subdir '{subdir}' not found in {github_repo}")
            shutil.copytree(subdir_path, install_path)
        except subprocess.TimeoutExpired:
            _mark_failed(conn, module_name, "git clone timed out after 120s")
            err(f"git clone timed out for {clone_url}")
        except FileNotFoundError:
            _mark_failed(conn, module_name, "git not found in PATH")
            err("git is not installed or not in PATH")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        # Standalone repo — clone directly
        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, install_path],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                _mark_failed(conn, module_name, f"git clone failed: {result.stderr.strip()}")
                err(f"Failed to clone {clone_url}: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            _mark_failed(conn, module_name, "git clone timed out after 120s")
            err(f"git clone timed out for {clone_url}")
        except FileNotFoundError:
            _mark_failed(conn, module_name, "git not found in PATH")
            err("git is not installed or not in PATH")

    # Full-tree integrity verification: every file in the registry's
    # files_sha256 manifest must exist in the fetched tree and hash to
    # the expected value. Mismatch, missing files, OR extra files cause
    # abort + cleanup.
    manifest = module_info.get("files_sha256")
    if manifest:
        # Walk the fetched tree to discover what was delivered, applying the
        # same skip filters used at manifest generation time. Canonical
        # source: `erpclaw_lib.skip_filters` (shared with
        # `release/regen_module_manifests.py` so the two walks can't drift).
        from erpclaw_lib.skip_filters import (
            SKIP_DIRS, SKIP_SUFFIXES, SKIP_FILE_EXACT,
        )
        # Files excluded from manifest by design (self-referential)
        SKIP_RELPATHS = (
            {"scripts/module_registry.json", "scripts/module_registry.json.sig",
             "scripts/signing_log.txt"}
            if module_name == "erpclaw" else set()
        )
        delivered = set()
        for root, dirs, files in os.walk(install_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if fname in SKIP_FILE_EXACT:
                    continue
                if any(fname.endswith(s) for s in SKIP_SUFFIXES):
                    continue
                rel = os.path.relpath(os.path.join(root, fname), install_path)
                if rel in SKIP_RELPATHS:
                    continue
                delivered.add(rel)

        expected = set(manifest.keys())
        missing = expected - delivered
        extra = delivered - expected
        mismatched = []

        for rel in sorted(expected & delivered):
            with open(os.path.join(install_path, rel), "rb") as f:
                actual = hashlib.sha256(f.read()).hexdigest()
            if actual != manifest[rel]:
                mismatched.append(rel)

        if missing or extra or mismatched:
            shutil.rmtree(install_path, ignore_errors=True)
            problems = []
            if missing:
                problems.append(f"missing: {sorted(missing)[:5]}")
            if extra:
                problems.append(f"extra: {sorted(extra)[:5]}")
            if mismatched:
                problems.append(f"mismatched: {mismatched[:5]}")
            summary = "; ".join(problems)
            _mark_failed(conn, module_name, f"integrity-check failed: {summary}")
            err(
                f"Integrity check failed for {module_name}: "
                f"{len(mismatched)} mismatched, {len(missing)} missing, "
                f"{len(extra)} extra files vs registry manifest. "
                f"Refusing to install. Details: {summary}"
            )

    # Read module.json if it exists
    module_json_path = os.path.join(install_path, "module.json")
    module_meta = {}
    if os.path.isfile(module_json_path):
        try:
            with open(module_json_path, "r") as f:
                module_meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # Non-fatal — registry info is sufficient

    # Merge metadata: module.json overrides registry defaults
    display_name = module_meta.get("display_name", module_info.get("display_name", module_name))
    version = module_meta.get("version", module_info.get("version", "0.0.0"))

    # Run init_db.py if it exists
    init_db_path = os.path.join(install_path, "init_db.py")
    tables_created = 0
    if os.path.isfile(init_db_path):
        try:
            # Set PYTHONPATH so init_db.py can find erpclaw_lib
            env = os.environ.copy()
            lib_path = lib_dir()
            env["PYTHONPATH"] = lib_path + os.pathsep + env.get("PYTHONPATH", "")
            result = subprocess.run(
                [sys.executable, init_db_path],
                capture_output=True, text=True, timeout=60,
                env=env,
            )
            if result.returncode != 0:
                # init_db.py may write its summary to stderr (intentional in
                # some modules, e.g. healthclaw) — fall back to stdout so the
                # operator sees the actual cause, not just an empty error.
                err_text = (result.stderr or "").strip() or (result.stdout or "").strip() or "(no output)"
                _mark_failed(conn, module_name, f"init_db.py failed: {err_text}")
                err(f"init_db.py failed for {module_name}: {err_text}")
        except subprocess.TimeoutExpired:
            _mark_failed(conn, module_name, "init_db.py timed out after 60s")
            err(f"init_db.py timed out for {module_name}")

        # Authoritative table count: query the DB after init_db.py finishes.
        # Parsing init_db.py output is unreliable across modules (some print to
        # stderr, some use "Tables: N" rather than "N tables", JSON is rare),
        # and the historical regex `(\d+)\s+tables?` silently missed
        # healthclaw/constructclaw/foundation outputs — install_module would
        # report tables_created=0 even when 59 tables were just created.
        # Commit any pending registry writes first so the fresh connection
        # below sees a consistent DB snapshot.
        try:
            conn.commit()
        except sqlite3.Error:
            pass
        try:
            data_db = db_default()
            if os.path.isfile(data_db):
                dconn = sqlite3.connect(data_db)
                prefix = module_name.replace("-", "_") + "_"
                cursor = dconn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE ?",
                    (prefix + "%",))
                tables_created = cursor.fetchone()[0]
                dconn.close()
        except sqlite3.Error as e:
            # Surface the failure instead of burying it (this was a silent
            # swallow in the prior implementation; install would report 0 with
            # no indication anything went wrong).
            print(f"WARN: install_module could not count {module_name} tables: {e}", file=sys.stderr)

    # Apply the module's own migrations (P1 — additive/alter changes init_db can't make)
    try:
        applied_migs = _run_module_migrations(module_name, install_path)
        if applied_migs:
            print(f"  applied {len(applied_migs)} migration(s) for {module_name}: "
                  f"{', '.join(applied_migs)}", file=sys.stderr)
    except Exception as e:
        _mark_failed(conn, module_name, f"module migration failed: {e}")
        err(f"module migration failed for {module_name}: {e}")

    # Build action cache
    action_count = build_action_cache(conn, module_name, install_path)

    # If no actions found, try scanning subdirectories (grouped repos like erpclaw-ops)
    if action_count == 0:
        scripts_dir = os.path.join(install_path, "scripts")
        if os.path.isdir(scripts_dir):
            for subdir in os.listdir(scripts_dir):
                sub_script = os.path.join(scripts_dir, subdir, "db_query.py")
                if os.path.isfile(sub_script):
                    sub_actions = _extract_actions_via_ast(sub_script)
                    if not sub_actions:
                        sub_actions = _extract_actions_via_regex(sub_script)
                    if sub_actions:
                        conn.executemany(
                            "INSERT OR REPLACE INTO erpclaw_module_action (module_name, action_name) VALUES (?, ?)",
                            [(module_name, a) for a in sorted(sub_actions)]
                        )
                        action_count += len(sub_actions)
            if action_count > 0:
                conn.commit()

    # Get git commit hash
    git_commit = _get_git_commit(install_path)

    # Update module record to installed
    conn.execute(
        """UPDATE erpclaw_module
           SET display_name = ?, version = ?, install_status = 'installed',
               git_commit = ?, tables_created = ?, action_count = ?,
               updated_at = ?, error_log = NULL
           WHERE name = ?""",
        (display_name, version, git_commit, tables_created, action_count, now, module_name)
    )
    conn.commit()

    # Regenerate SKILL.md with new module actions
    _regenerate_skill_md(conn)

    # Publish to OpenClaw's workspace skills dir so the agent can find it.
    # See OPENCLAW_WORKSPACE_SKILLS_DIR comment at the top of this file.
    workspace_published = _publish_to_openclaw_skills(install_path, module_name)

    return {
        "module": module_name,
        "display_name": display_name,
        "version": version,
        "action_count": action_count,
        "tables_created": tables_created,
        "install_path": install_path,
        "workspace_skill_path": workspace_published,
        "git_commit": git_commit,
        "installed_at": now,
    }


def _publish_to_openclaw_skills(install_path, module_name):
    """Copy an installed module into ~/.openclaw/workspace/skills/<module>/.

    OpenClaw's agent only sees skills under workspace/skills, not under
    erpclaw/modules. We mirror each install into both so the agent can
    invoke module actions (e.g., shopify-status). Best-effort: returns
    the published path on success, None if OpenClaw isn't installed
    (workspace dir missing) or if the copy fails. We never raise, since
    the module install itself was already successful.
    """
    if not os.path.isdir(os.path.dirname(OPENCLAW_WORKSPACE_SKILLS_DIR)):
        # OpenClaw not installed on this host. Nothing to do.
        return None
    try:
        os.makedirs(OPENCLAW_WORKSPACE_SKILLS_DIR, exist_ok=True)
        dest = os.path.join(OPENCLAW_WORKSPACE_SKILLS_DIR, module_name)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.copytree(install_path, dest)
        return dest
    except (OSError, shutil.Error):
        # Don't fail the whole install over this. Surface via the
        # returned None so callers + tests can react if they want.
        return None


def _run_module_migrations(module_name, install_path):
    """Run a module's own migrations/NNN_*.py (P1 — module schema evolution).

    init_db.py is CREATE-TABLE-IF-NOT-EXISTS only, so it cannot alter an existing
    table on upgrade. A module that needs to add a column / table to an installed
    DB ships migrations/NNN_*.py; this applies the pending ones via the foundation
    runner, recorded under the module's name in the shared ledger. No-op if the
    module has no migrations/ dir. Returns the list of applied migration stems.
    """
    migrations_dir = os.path.join(install_path, "migrations")
    if not os.path.isdir(migrations_dir):
        return []
    runner_path = os.path.join(SCRIPT_DIR, "erpclaw-setup", "migration_runner.py")
    if not os.path.isfile(runner_path):
        return []
    import importlib.util
    spec = importlib.util.spec_from_file_location("migration_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    data_db = db_default()
    res = runner.run_pending(data_db, migrations_dir=migrations_dir, module_name=module_name)
    if res.get("ok") is False:
        raise RuntimeError(
            f"module migration '{res['failed']}' failed for {module_name}: {res['error']}")
    return res.get("applied", [])


def _run_foundation_migrations():
    """Apply pending foundation (erpclaw-setup) migrations after a successful
    update-foundation reconcile (M31 H1 / FINDINGS B1). Mirrors
    _run_module_migrations but targets the foundation's own migrations/ dir, so a
    reconcile that lands new DDL-bearing migration files also runs them instead of
    silently shipping schema the DB never applied.

    Returns a JSON-safe dict (never raises — a runner problem must not mask the
    file reconcile the caller already completed):
      - {"ran": False, "reason": ...}                       migrations/runner absent
      - {"ran": True, "ok": True,  "applied": [...], ...}    all pending applied
      - {"ran": True, "ok": False, "failed": stem, ...}      a migration raised; the
        runner recorded ledger state ('failed' + each 'applied') and left the DB at
        the last good migration. The caller surfaces this loudly and exits non-zero.
    """
    migrations_dir = os.path.join(SCRIPT_DIR, "erpclaw-setup", "migrations")
    runner_path = os.path.join(SCRIPT_DIR, "erpclaw-setup", "migration_runner.py")
    if not os.path.isdir(migrations_dir) or not os.path.isfile(runner_path):
        return {"ran": False, "reason": "no foundation migrations dir / runner present"}
    import importlib.util
    spec = importlib.util.spec_from_file_location("migration_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(runner)
    except Exception as e:  # noqa: BLE001 — surface, never mask the reconcile
        return {"ran": True, "ok": False, "failed": "<runner-import>", "applied": [],
                "error": str(e), "detail": "migration_runner failed to import"}

    db_path = db_default()
    # Migrations UPGRADE an already-initialized foundation DB (fresh installs get
    # the current schema from init_schema). Running ALTERs against an absent or
    # schema-less DB is nonsensical, so skip cleanly when the core schema is not
    # present — dialect-agnostic via the runner's own _connect.
    if not _foundation_db_initialized(runner, db_path):
        return {"ran": False, "reason": "foundation DB not initialized (nothing to migrate)"}

    try:
        res = runner.run_pending(
            db_path, migrations_dir=migrations_dir, module_name="erpclaw-setup")
    except Exception as e:  # noqa: BLE001 — surface, never mask the reconcile
        return {"ran": True, "ok": False, "failed": "<runner>", "applied": [],
                "error": str(e), "detail": "migration_runner failed to execute"}
    res["ran"] = True
    return res


def _foundation_db_initialized(runner, db_path):
    """True if ``db_path`` is an initialized foundation DB (has the core schema).

    Dialect-agnostic: uses the runner's own ``_connect`` so SQLite and Postgres
    both work, and probes a core table (``company``). Any error — absent DB,
    missing table, connect failure — means "not initialized" → skip migrations.
    A non-existent SQLite path short-circuits so we never create a stray empty DB.
    """
    if "://" not in str(db_path) and not os.path.isfile(db_path):
        return False
    try:
        conn, _ = runner._connect(db_path)
    except Exception:  # noqa: BLE001
        return False
    try:
        conn.cursor().execute("SELECT 1 FROM company LIMIT 1")
        return True
    except Exception:  # noqa: BLE001 — missing table / uninitialized
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _mark_failed(conn, module_name, error_msg):
    """Mark a module installation as failed."""
    conn.execute(
        "UPDATE erpclaw_module SET install_status = 'failed', error_log = ?, updated_at = ? WHERE name = ?",
        (error_msg, _now_iso(), module_name)
    )
    conn.commit()


def _cleanup_failed_install(conn, module_name):
    """Remove records from a previously failed installation."""
    row = conn.execute(
        "SELECT install_path FROM erpclaw_module WHERE name = ?",
        (module_name,)
    ).fetchone()
    if row and row["install_path"] and os.path.isdir(os.path.expanduser(row["install_path"])):
        shutil.rmtree(os.path.expanduser(row["install_path"]), ignore_errors=True)
    conn.execute("DELETE FROM erpclaw_module_action WHERE module_name = ?", (module_name,))
    conn.execute("DELETE FROM erpclaw_module WHERE name = ?", (module_name,))
    conn.commit()


# ---------------------------------------------------------------------------
# Action: remove-module
# ---------------------------------------------------------------------------

def remove_module(args):
    """Remove an installed module.

    Checks that no other installed modules depend on this one before removal.
    Deletes the module directory and database records, but preserves any
    tables/data the module created (no DROP TABLE).
    """
    module_name = args.module_name
    if not module_name:
        err("--module-name is required")

    conn = get_connection()

    # Check module exists
    row = conn.execute(
        "SELECT * FROM erpclaw_module WHERE name = ?",
        (module_name,)
    ).fetchone()
    if not row:
        err(f"Module '{module_name}' is not installed")

    # Check reverse dependencies — are any other installed modules depending on this one?
    installed = _get_installed_modules(conn)
    dependents = []
    for name, mod in installed.items():
        if name == module_name:
            continue
        requires = json.loads(mod.get("requires_json") or "[]")
        if module_name in requires:
            dependents.append(name)

    if dependents:
        err(
            f"Cannot remove '{module_name}': required by {', '.join(dependents)}",
            suggestion=f"Remove dependent modules first: {', '.join(dependents)}"
        )

    install_path = os.path.expanduser(row["install_path"])

    # Mark as removing
    conn.execute(
        "UPDATE erpclaw_module SET install_status = 'removing', updated_at = ? WHERE name = ?",
        (_now_iso(), module_name)
    )
    conn.commit()

    # Delete action cache
    conn.execute("DELETE FROM erpclaw_module_action WHERE module_name = ?", (module_name,))

    # Delete module record
    conn.execute("DELETE FROM erpclaw_module WHERE name = ?", (module_name,))
    conn.commit()

    # Remove directory
    if install_path and os.path.isdir(install_path):
        shutil.rmtree(install_path, ignore_errors=True)

    # Also remove the workspace-skills mirror so OpenClaw stops listing it.
    workspace_dest = os.path.join(OPENCLAW_WORKSPACE_SKILLS_DIR, module_name)
    if os.path.isdir(workspace_dest):
        shutil.rmtree(workspace_dest, ignore_errors=True)

    # Regenerate SKILL.md without removed module
    _regenerate_skill_md(conn)

    ok({
        "module": module_name,
        "removed": True,
        "note": "Module directory and records removed. Database tables are preserved.",
    })


# ---------------------------------------------------------------------------
# Action: update-modules
# ---------------------------------------------------------------------------

def update_modules(args):
    """Update all or a specific installed module.

    For each module: fetches from origin, compares HEAD with origin/main,
    pulls if different, re-runs init_db.py, and rebuilds the action cache.
    """
    conn = get_connection()
    target_name = getattr(args, "module_name", None)

    if target_name:
        rows = conn.execute(
            "SELECT * FROM erpclaw_module WHERE name = ? AND install_status = 'installed'",
            (target_name,)
        ).fetchall()
        if not rows:
            err(f"Module '{target_name}' is not installed or not in 'installed' state")
    else:
        rows = conn.execute(
            "SELECT * FROM erpclaw_module WHERE install_status = 'installed'"
        ).fetchall()

    if not rows:
        ok({"updated": [], "message": "No modules to update"})

    updated = []
    skipped = []
    failed = []

    for row in rows:
        module_name = row["name"]
        install_path = os.path.expanduser(row["install_path"])

        if not os.path.isdir(install_path):
            failed.append({"module": module_name, "error": "Install directory missing"})
            continue

        has_updates, local_commit, remote_commit = _check_remote_updates(install_path)

        if not has_updates:
            skipped.append({"module": module_name, "commit": local_commit, "reason": "already up to date"})
            continue

        # Mark as updating
        conn.execute(
            "UPDATE erpclaw_module SET install_status = 'updating', updated_at = ? WHERE name = ?",
            (_now_iso(), module_name)
        )
        conn.commit()

        # Pull latest
        try:
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=install_path, capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                _mark_failed(conn, module_name, f"git pull failed: {result.stderr.strip()}")
                failed.append({"module": module_name, "error": f"git pull failed: {result.stderr.strip()}"})
                continue
        except subprocess.TimeoutExpired:
            _mark_failed(conn, module_name, "git pull timed out")
            failed.append({"module": module_name, "error": "git pull timed out"})
            continue

        # Re-run init_db.py if it exists (creates any NEW tables)
        init_db_path = os.path.join(install_path, "init_db.py")
        if os.path.isfile(init_db_path):
            try:
                subprocess.run(
                    [sys.executable, init_db_path],
                    capture_output=True, text=True, timeout=60
                )
            except subprocess.TimeoutExpired:
                pass  # Non-fatal for updates

        # Apply the module's pending migrations (P1 — the path that actually
        # evolves an EXISTING table on upgrade; init_db re-run can't alter tables)
        try:
            applied_migs = _run_module_migrations(module_name, install_path)
            if applied_migs:
                print(f"  {module_name}: applied {len(applied_migs)} migration(s) "
                      f"on update: {', '.join(applied_migs)}", file=sys.stderr)
        except Exception as e:
            _mark_failed(conn, module_name, f"module migration failed on update: {e}")
            failed.append({"module": module_name, "error": f"module migration failed: {e}"})
            continue

        # Read updated module.json
        module_json_path = os.path.join(install_path, "module.json")
        new_version = row["version"]
        if os.path.isfile(module_json_path):
            try:
                with open(module_json_path, "r") as f:
                    meta = json.load(f)
                new_version = meta.get("version", new_version)
            except (json.JSONDecodeError, OSError):
                pass

        # Rebuild action cache
        action_count = build_action_cache(conn, module_name, install_path)
        new_commit = _get_git_commit(install_path)
        now = _now_iso()

        conn.execute(
            """UPDATE erpclaw_module
               SET install_status = 'installed', version = ?, git_commit = ?,
                   action_count = ?, updated_at = ?, error_log = NULL
               WHERE name = ?""",
            (new_version, new_commit, action_count, now, module_name)
        )
        conn.commit()

        updated.append({
            "module": module_name,
            "old_commit": local_commit,
            "new_commit": new_commit,
            "version": new_version,
            "action_count": action_count,
        })

    ok({
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "summary": f"{len(updated)} updated, {len(skipped)} up-to-date, {len(failed)} failed",
    })


# ---------------------------------------------------------------------------
# Action: list-modules
# ---------------------------------------------------------------------------

def list_modules(args):
    """List all installed and active modules."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT name, display_name, version, category, action_count,
                  tables_created, installed_at, updated_at, git_commit, install_status
           FROM erpclaw_module
           WHERE is_active = 1
           ORDER BY category, name"""
    ).fetchall()

    modules = rows_to_list(rows)

    # Enrich with action count from cache (authoritative source)
    for mod in modules:
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM erpclaw_module_action WHERE module_name = ?",
            (mod["name"],)
        ).fetchone()
        mod["cached_actions"] = count["cnt"] if count else 0

    ok({
        "modules": modules,
        "total": len(modules),
    })


# ---------------------------------------------------------------------------
# Action: available-modules
# ---------------------------------------------------------------------------

def available_modules(args):
    """Browse the module catalog, cross-referenced with install status."""
    refresh = getattr(args, "refresh", False)
    registry = _load_registry(force_refresh=refresh)
    conn = get_connection()
    installed = _get_installed_modules(conn)

    category_filter = getattr(args, "category", None)
    search_query = getattr(args, "search", None)

    results = []
    for mod in _registry_to_dict(registry).values():
        # Filter by category
        if category_filter and mod.get("category") != category_filter:
            continue

        # Filter by search query
        if search_query:
            query_lower = search_query.lower()
            searchable = " ".join([
                mod.get("name", ""),
                mod.get("display_name", ""),
                mod.get("description", ""),
                " ".join(mod.get("tags", [])),
            ]).lower()
            if query_lower not in searchable:
                continue

        entry = {
            "name": mod["name"],
            "display_name": mod.get("display_name", mod["name"]),
            "description": mod.get("description", ""),
            "category": mod.get("category", "expansion"),
            "version": mod.get("version", "0.0.0"),
            "tags": mod.get("tags", []),
            "requires": mod.get("requires", []),
        }

        # Cross-reference with installed modules
        if mod["name"] in installed:
            inst = installed[mod["name"]]
            entry["installed"] = True
            entry["installed_version"] = inst["version"]
            entry["install_status"] = inst["install_status"]
        else:
            entry["installed"] = False

        results.append(entry)

    ok({
        "modules": results,
        "total": len(results),
        "filters": {
            "category": category_filter,
            "search": search_query,
        },
    })


# ---------------------------------------------------------------------------
# Action: module-status
# ---------------------------------------------------------------------------

def module_status(args):
    """Show detailed status for a specific installed module."""
    module_name = args.module_name
    if not module_name:
        err("--module-name is required")

    conn = get_connection()

    row = conn.execute(
        "SELECT * FROM erpclaw_module WHERE name = ?",
        (module_name,)
    ).fetchone()
    if not row:
        err(f"Module '{module_name}' is not installed")

    mod = row_to_dict(row)
    install_path = os.path.expanduser(mod["install_path"])

    # Get cached actions
    actions = conn.execute(
        "SELECT action_name FROM erpclaw_module_action WHERE module_name = ? ORDER BY action_name",
        (module_name,)
    ).fetchall()
    mod["actions"] = [a["action_name"] for a in actions]
    mod["cached_action_count"] = len(mod["actions"])

    # Parse requires_json
    mod["requires"] = json.loads(mod.get("requires_json") or "[]")
    del mod["requires_json"]

    # Check for dependents (who depends on this module)
    installed = _get_installed_modules(conn)
    dependents = []
    for name, inst in installed.items():
        if name == module_name:
            continue
        requires = json.loads(inst.get("requires_json") or "[]")
        if module_name in requires:
            dependents.append(name)
    mod["dependents"] = dependents

    # Check git status
    if os.path.isdir(install_path):
        mod["directory_exists"] = True
        has_updates, local_commit, remote_commit = _check_remote_updates(install_path)
        mod["has_updates"] = has_updates
        mod["local_commit"] = local_commit
        mod["remote_commit"] = remote_commit
    else:
        mod["directory_exists"] = False
        mod["has_updates"] = None
        mod["local_commit"] = None
        mod["remote_commit"] = None

    ok(mod)


# ---------------------------------------------------------------------------
# Action: search-modules
# ---------------------------------------------------------------------------

def search_modules(args):
    """Search the module catalog by name, description, and tags."""
    search_query = getattr(args, "search", None)
    if not search_query:
        err("--search is required")

    refresh = getattr(args, "refresh", False)
    registry = _load_registry(force_refresh=refresh)
    query_lower = search_query.lower()
    query_terms = query_lower.split()

    results = []
    for mod in _registry_to_dict(registry).values():
        searchable = " ".join([
            mod.get("name", ""),
            mod.get("display_name", ""),
            mod.get("description", ""),
            " ".join(mod.get("tags", [])),
        ]).lower()

        # All terms must match
        if all(term in searchable for term in query_terms):
            results.append({
                "name": mod["name"],
                "display_name": mod.get("display_name", mod["name"]),
                "description": mod.get("description", ""),
                "category": mod.get("category", "expansion"),
                "version": mod.get("version", "0.0.0"),
                "tags": mod.get("tags", []),
            })

    ok({
        "query": search_query,
        "results": results,
        "total": len(results),
    })


# ---------------------------------------------------------------------------
# Action: rebuild-action-cache
# ---------------------------------------------------------------------------

def rebuild_action_cache(args):
    """Rebuild the entire action cache from all installed modules.

    Truncates erpclaw_module_action and re-scans every installed module's
    db_query.py. Useful after migrations, manual changes, or cache corruption.
    """
    conn = get_connection()

    # Clear entire cache
    conn.execute("DELETE FROM erpclaw_module_action")
    conn.commit()

    rows = conn.execute(
        "SELECT name, install_path FROM erpclaw_module WHERE install_status = 'installed'"
    ).fetchall()

    rebuilt = []
    errors = []
    total_actions = 0

    for row in rows:
        module_name = row["name"]
        install_path = os.path.expanduser(row["install_path"])

        if not os.path.isdir(install_path):
            errors.append({"module": module_name, "error": "Install directory missing"})
            continue

        try:
            count = build_action_cache(conn, module_name, install_path)
            # Update the action_count in the module record
            conn.execute(
                "UPDATE erpclaw_module SET action_count = ?, updated_at = ? WHERE name = ?",
                (count, _now_iso(), module_name)
            )
            conn.commit()
            rebuilt.append({"module": module_name, "action_count": count})
            total_actions += count
        except Exception as e:
            errors.append({"module": module_name, "error": str(e)})

    # Regenerate SKILL.md with updated actions
    _regenerate_skill_md(conn)

    ok({
        "rebuilt": rebuilt,
        "errors": errors,
        "total_modules": len(rebuilt),
        "total_actions": total_actions,
        "summary": f"Rebuilt cache for {len(rebuilt)} modules ({total_actions} actions), {len(errors)} errors",
    })


# ---------------------------------------------------------------------------
# Action: list-all-actions
# ---------------------------------------------------------------------------

def list_all_actions(args):
    """Return all available actions — core + installed modules."""
    conn = get_connection()

    # Get core actions from the main ACTION_MAP
    # We need to read db_query.py to get the ACTION_MAP keys
    db_query_path = os.path.join(SCRIPT_DIR, "db_query.py")
    core_actions = _extract_actions_via_regex(db_query_path)
    # Also add MODULE_ACTIONS and ONBOARDING_ACTIONS
    core_actions |= {
        "install-module", "remove-module", "update-modules",
        "list-modules", "available-modules", "module-status",
        "search-modules", "rebuild-action-cache",
        "list-profiles", "onboard", "list-all-actions",
    }

    # Module actions from cache
    rows = conn.execute(
        """SELECT ma.action_name, ma.module_name
           FROM erpclaw_module_action ma
           JOIN erpclaw_module m ON m.name = ma.module_name
           WHERE m.install_status = 'installed' AND m.is_active = 1
           ORDER BY ma.module_name, ma.action_name"""
    ).fetchall()

    module_actions = {}
    for r in rows:
        module_actions.setdefault(r["module_name"], []).append(r["action_name"])

    ok({
        "core_actions": sorted(core_actions),
        "core_count": len(core_actions),
        "module_actions": module_actions,
        "module_count": len(module_actions),
        "total": len(core_actions) + sum(len(v) for v in module_actions.values()),
    })


# ---------------------------------------------------------------------------
# Action: regenerate-skill-md
# ---------------------------------------------------------------------------

def regenerate_skill_md_action(args):
    """Regenerate the deployed SKILL.md with installed module actions."""
    conn = get_connection()
    _regenerate_skill_md(conn)

    # Count what was generated
    rows = conn.execute(
        """SELECT m.name, COUNT(ma.action_name) as cnt
           FROM erpclaw_module m
           LEFT JOIN erpclaw_module_action ma ON m.name = ma.module_name
           WHERE m.install_status = 'installed' AND m.is_active = 1
           GROUP BY m.name"""
    ).fetchall()

    modules = [{"module": r["name"], "actions": r["cnt"]} for r in rows]
    ok({
        "regenerated": True,
        "modules": modules,
        "total_modules": len(modules),
        "deployed_path": os.path.expanduser("~/clawd/skills/erpclaw/SKILL.md"),
    })


# ---------------------------------------------------------------------------
# Action dispatch and CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Foundation source synchronization
# ---------------------------------------------------------------------------

def _sync_log(msg):
    """Append timestamped line to sync log; never raise."""
    try:
        os.makedirs(os.path.dirname(SYNC_LOG_PATH), exist_ok=True)
        with open(SYNC_LOG_PATH, "a") as f:
            f.write(f"[{_now_iso()}] {msg}\n")
    except Exception:
        pass


def _acquire_sync_lock():
    """Open and flock the sync lock file. Returns file handle or None on contention."""
    import fcntl
    try:
        os.makedirs(os.path.dirname(SYNC_LOCK_PATH), exist_ok=True)
        fh = open(SYNC_LOCK_PATH, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (BlockingIOError, OSError):
        try:
            fh.close()
        except Exception:
            pass
        return None


def _release_sync_lock(fh):
    """Release lock acquired via _acquire_sync_lock."""
    if fh is None:
        return
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _walk_foundation_tree():
    """Yield install-relative paths in the foundation tree, applying skip filters.

    Mirrors the filter set used by install_module's full-tree verification, so
    drift detection and verification agree on which files are in scope.
    """
    for root, dirs, files in os.walk(FOUNDATION_INSTALL_ROOT):
        dirs[:] = [d for d in dirs if d not in SYNC_SKIP_DIRS]
        for fname in files:
            if any(fname.endswith(s) for s in SYNC_SKIP_SUFFIXES):
                continue
            if fname in SYNC_SKIP_BASENAMES_FOUNDATION:
                continue
            rel = os.path.relpath(os.path.join(root, fname), FOUNDATION_INSTALL_ROOT)
            if rel in SYNC_SKIP_RELPATHS_FOUNDATION:
                continue
            yield rel


def _hash_file(path):
    """Return hex SHA256 of file contents, or None if unreadable."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _compute_foundation_drift(manifest):
    """Compare local install tree to manifest. Returns dict with drift sets.

    {
        "modified": [relpath, ...],   # exists locally + manifest, hash differs
        "missing":  [relpath, ...],   # in manifest, not on disk
        "orphaned": [relpath, ...],   # on disk, not in manifest
    }
    """
    expected = set(manifest.keys())
    delivered = set(_walk_foundation_tree())

    missing = sorted(expected - delivered)
    orphaned = sorted(delivered - expected)

    modified = []
    for rel in sorted(expected & delivered):
        local_hash = _hash_file(os.path.join(FOUNDATION_INSTALL_ROOT, rel))
        if local_hash is None or local_hash != manifest[rel]:
            modified.append(rel)

    return {"modified": modified, "missing": missing, "orphaned": orphaned}


def _fetch_remote_file(rel_path, expected_hash):
    """Fetch a single file from GitHub raw and verify SHA256.

    Returns bytes on success, raises on hash mismatch or fetch failure.
    Honors ERPCLAW_GITHUB_RAW_BASE for test-mode redirection to a local mirror.
    """
    base = os.environ.get("ERPCLAW_GITHUB_RAW_BASE", GITHUB_RAW_BASE)
    url = f"{base}/{rel_path}"
    req = urllib.request.Request(url, headers={"User-Agent": "erpclaw"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_hash:
        raise ValueError(
            f"hash mismatch for {rel_path}: expected {expected_hash[:12]}, "
            f"got {actual[:12]}"
        )
    return data


def _atomic_write(target_path, data):
    """Write data to target_path atomically.

    Preserves:
      * Prior content as .bak (one cycle, used by rollback-foundation).
      * File mode of the existing target (so executable bits on shipped
        scripts like bin/erpclaw survive reconciliation; without this
        the atomic replace would write the new file at the umask default
        mode and silently break invocations like `bin/erpclaw --version`).
    """
    target_dir = os.path.dirname(target_path) or "."
    os.makedirs(target_dir, exist_ok=True)
    tmp = target_path + ".new"
    bak = target_path + ".bak"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if os.path.isfile(target_path):
        try:
            shutil.copymode(target_path, tmp)
        except OSError:
            pass
        try:
            shutil.copy2(target_path, bak)
        except OSError:
            pass
    os.replace(tmp, target_path)


def _is_dev_source_tree(path):
    """True if `path` is a developer's git checkout, not a clawhub-installed skill.

    Reconciliation must never overwrite a developer's source checkout of the
    foundation.

    ClawHub CLI v0.12.3 installs a skill by FILE EXTRACTION (it unpacks the
    published package into the skill dir); the install is NOT a git repository,
    so `git ls-files SKILL.md` returns non-zero and this function returns False
    — correctly classifying an extracted install as reconcilable, not a dev
    tree. (Older CLI builds git-cloned avansaber/erpclaw; the remote heuristic
    below keeps those safe to reconcile too.)

    We therefore can't classify by "is SKILL.md tracked by some git repo" alone,
    because a legacy clawhub git-clone's SKILL.md IS tracked. Distinguishing
    signal for the tracked case: the git remote URL.
      - Legacy ClawHub git-clone: single `origin` at github.com/avansaber/erpclaw
      - Dev checkout: remote points at the private monorepo, a fork, or has
        no remote, or has multiple remotes that include a non-avansaber one.

    A path is "dev tree" only when SKILL.md is tracked AND the remote set is
    NOT exclusively the canonical avansaber/erpclaw upstream. That keeps both
    extracted and legacy-clone clawhub deployments safe to reconcile while still
    refusing to stomp on any developer's working copy.
    """
    skill_md = os.path.join(path, "SKILL.md")
    if not os.path.isfile(skill_md):
        return False
    try:
        tracked = subprocess.run(
            ["git", "-C", path, "ls-files", "--error-unmatch", "SKILL.md"],
            capture_output=True, timeout=5,
        )
        if tracked.returncode != 0:
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    # SKILL.md is tracked. Inspect remotes to decide dev vs prod.
    try:
        remotes = subprocess.run(
            ["git", "-C", path, "remote", "-v"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True  # tracked but git inspection failed; be conservative
    if remotes.returncode != 0:
        return True
    urls = set()
    for line in remotes.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.add(parts[1])
    if not urls:
        return True  # tracked locally but no remote at all
    canonical_markers = ("avansaber/erpclaw.git", "avansaber/erpclaw")
    is_canonical_only = all(
        any(marker in u for marker in canonical_markers) for u in urls
    )
    return not is_canonical_only


def update_foundation_action(args):
    """Synchronize installed foundation files with the registry manifest.

    Idempotent. No-op when local hashes match. Per-file atomic replace with
    one-cycle .bak preserved for rollback. Pre-flight downloads + verifies
    all drifting files before any rename, so a failure mid-fetch leaves the
    install untouched.

    Confirmation gate is enforced at the foundation router; this function
    assumes a trusted entry point.
    """
    if _is_dev_source_tree(FOUNDATION_INSTALL_ROOT):
        err(
            "Refusing to sync a git-tracked source tree. "
            "Auto-sync targets ClawHub-installed deployments, not development checkouts."
        )

    if not os.access(FOUNDATION_INSTALL_ROOT, os.W_OK):
        err(
            f"Foundation install path is not writable: {FOUNDATION_INSTALL_ROOT}. "
            f"Run with sufficient permissions or relocate install."
        )

    lock = _acquire_sync_lock()
    if lock is None:
        err("Another foundation sync is in progress; try again shortly.")

    try:
        # Strict load: ed25519 signature verified, monotonic version checked.
        # Refuses unsigned, tampered, or downgraded registries.
        unsafe = getattr(args, "unsafe_trust_bundled", False)
        if unsafe:
            print("WARNING: --unsafe-trust-bundled set; skipping signature verification.",
                  file=sys.stderr)
            # Honor cache when in unsafe mode so emergency recovery and tests
            # can use locally-staged registries.
            registry = _load_registry(force_refresh=False)
        else:
            try:
                registry = _load_registry_strict(force_refresh=True)
            except _RegistrySignatureError as e:
                err(
                    f"Registry signature verification failed: {e}. "
                    f"Refusing to reconcile. If this is an emergency, "
                    f"re-run with --unsafe-trust-bundled (NOT RECOMMENDED)."
                )
        modules_by_name = _registry_to_dict(registry)
        foundation = modules_by_name.get("erpclaw")
        if not foundation or "files_sha256" not in foundation:
            err("Registry has no erpclaw foundation manifest; refusing to sync.")
        manifest = foundation["files_sha256"]

        drift = _compute_foundation_drift(manifest)
        to_replace = drift["modified"] + drift["missing"]
        to_delete = drift["orphaned"]

        if not to_replace and not to_delete:
            try:
                with open(LAST_SYNC_MARKER, "w") as f:
                    f.write(_now_iso())
            except OSError:
                pass
            result = {
                "status": "ok",
                "version": foundation.get("version"),
                "in_sync": True,
                "modified": [], "missing": [], "orphaned": [],
            }
            # ADR-0028 / BDFL checkpoint ② condition (b): a confirmed apply-path
            # run converges files AND schema — the in-sync early return must not
            # silently skip pending migrations. Without this, the retry after a
            # files-ok/migration-failed run reports "ok, in_sync" while pending
            # DDL sits unapplied (the exact FINDINGS B1 silent-skip class this
            # wiring exists to kill). Idempotent no-op when nothing is pending.
            # NOTE the dry-run check sits BELOW this block, so guard explicitly:
            # previews must stay read-only.
            if not getattr(args, "dry_run", False):
                migrations = _run_foundation_migrations()
                _sync_log(f"foundation migrations (in-sync path): {migrations}")
                result["migrations"] = migrations
                if migrations.get("ran") and migrations.get("ok") is False:
                    result["status"] = "error"
                    result["message"] = (
                        f"Foundation files already in sync at "
                        f"{foundation.get('version')}, but migration "
                        f"'{migrations.get('failed')}' FAILED: "
                        f"{migrations.get('error')}. {migrations.get('detail', '')} "
                        f"Applied before failure: {migrations.get('applied', [])}."
                    )
                    print(json.dumps(result, indent=2))
                    sys.exit(1)
            print(json.dumps(result))
            return

        if getattr(args, "dry_run", False):
            print(json.dumps({
                "status": "ok",
                "version": foundation.get("version"),
                "in_sync": False,
                "would_replace": to_replace,
                "would_delete": to_delete,
            }))
            return

        # Pre-flight: download + verify all replacements before any rename
        staged = {}
        for rel in to_replace:
            try:
                staged[rel] = _fetch_remote_file(rel, manifest[rel])
            except Exception as e:
                _sync_log(f"fetch failed for {rel}: {e}")
                err(
                    f"Pre-flight fetch failed for {rel}; install untouched. "
                    f"Reason: {e}"
                )

        # Apply: atomic per-file replace
        replaced = []
        for rel, data in staged.items():
            target = os.path.join(FOUNDATION_INSTALL_ROOT, rel)
            try:
                _atomic_write(target, data)
                replaced.append(rel)
            except OSError as e:
                _sync_log(f"replace failed for {rel}: {e}")

        # Apply: delete orphans (files removed from manifest)
        deleted = []
        for rel in to_delete:
            target = os.path.join(FOUNDATION_INSTALL_ROOT, rel)
            try:
                if os.path.isfile(target):
                    bak = target + ".bak"
                    try:
                        shutil.copy2(target, bak)
                    except OSError:
                        pass
                    os.remove(target)
                    deleted.append(rel)
            except OSError as e:
                _sync_log(f"delete failed for {rel}: {e}")

        try:
            with open(LAST_SYNC_MARKER, "w") as f:
                f.write(_now_iso())
        except OSError:
            pass

        _sync_log(
            f"sync complete: replaced={len(replaced)} deleted={len(deleted)} "
            f"version={foundation.get('version')}"
        )

        # M31 H1 / FINDINGS B1 (BDFL checkpoint ②): a reconcile can land new
        # DDL-bearing migration files, so apply pending foundation migrations
        # here — an upgrade can never silently ship schema it never ran. This
        # runs only on the apply path (dry-run + in-sync returned above) and
        # inherits the reconcile's --user-confirmed dangerous-action gate, so it
        # is never triggered by a preview or an unconfirmed call. Loud on failure.
        migrations = _run_foundation_migrations()
        _sync_log(f"foundation migrations: {migrations}")

        result = {
            "status": "ok",
            "version": foundation.get("version"),
            "in_sync": True,
            "replaced": replaced,
            "deleted": deleted,
            "migrations": migrations,
        }
        if migrations.get("ran") and migrations.get("ok") is False:
            # Files reconciled, but a migration failed. Do NOT claim success: the
            # runner recorded ledger state (the failed stem + each applied one)
            # and left the DB at the last good migration. Surface loudly + exit 1
            # so operators and CI see the half-applied upgrade and re-run.
            result["status"] = "error"
            result["message"] = (
                f"Foundation files reconciled to {foundation.get('version')}, but "
                f"migration '{migrations.get('failed')}' FAILED: "
                f"{migrations.get('error')}. {migrations.get('detail', '')} "
                f"Applied before failure: {migrations.get('applied', [])}."
            )
            print(json.dumps(result, indent=2))
            sys.exit(1)

        print(json.dumps(result))
    finally:
        _release_sync_lock(lock)


def verify_trust_root_action(args):
    """Print embedded public key fingerprint(s) for out-of-band verification."""
    sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    try:
        from erpclaw_lib.signing import TRUSTED_KEYS, fingerprint
    except ImportError as e:
        err(f"signing library unavailable: {e}")

    keys = []
    for tk in TRUSTED_KEYS:
        keys.append({
            "label": tk.label,
            "fingerprint": fingerprint(tk.public_key_hex),
            "valid_until": tk.valid_until,
        })
    print(json.dumps({
        "status": "ok",
        "trusted_keys": keys,
        "note": "Verify these fingerprints against the published values on erpclaw.ai before trusting reconciliation.",
    }, indent=2))


def rollback_foundation_action(args):
    """Restore .bak copies preserved by the most recent update-foundation run.

    One-cycle rollback: each .bak holds the file's pre-sync state. Running
    rollback twice in a row is idempotent for files with no .bak.

    Confirmation gate is enforced at the foundation router; this function
    assumes a trusted entry point.
    """
    lock = _acquire_sync_lock()
    if lock is None:
        err("Another foundation sync is in progress; try again shortly.")

    try:
        restored = []
        skipped = []
        for root, dirs, files in os.walk(FOUNDATION_INSTALL_ROOT):
            dirs[:] = [d for d in dirs if d not in SYNC_SKIP_DIRS]
            for fname in files:
                if not fname.endswith(".bak"):
                    continue
                bak_path = os.path.join(root, fname)
                target_path = bak_path[:-4]  # strip .bak
                try:
                    shutil.copy2(bak_path, target_path)
                    os.remove(bak_path)
                    restored.append(os.path.relpath(target_path, FOUNDATION_INSTALL_ROOT))
                except OSError as e:
                    skipped.append({
                        "path": os.path.relpath(bak_path, FOUNDATION_INSTALL_ROOT),
                        "reason": str(e),
                    })

        _sync_log(f"rollback complete: restored={len(restored)} skipped={len(skipped)}")
        print(json.dumps({
            "status": "ok",
            "restored": restored,
            "skipped": skipped,
        }))
    finally:
        _release_sync_lock(lock)


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

ACTIONS = {
    "install-module": install_module,
    "remove-module": remove_module,
    "update-modules": update_modules,
    "list-modules": list_modules,
    "available-modules": available_modules,
    "module-status": module_status,
    "search-modules": search_modules,
    "rebuild-action-cache": rebuild_action_cache,
    "list-all-actions": list_all_actions,
    "regenerate-skill-md": regenerate_skill_md_action,
    "update-foundation": lambda args: update_foundation_action(args),
    "rollback-foundation": lambda args: rollback_foundation_action(args),
    "verify-trust-root": lambda args: verify_trust_root_action(args),
}


def main():
    parser = argparse.ArgumentParser(
        description="ERPClaw Module Manager — install, update, and manage expansion modules"
    )
    parser.add_argument(
        "--action", required=True, choices=sorted(ACTIONS.keys()),
        help="Action to perform"
    )
    parser.add_argument(
        "--module-name",
        help="Module name (for install, remove, update, status)"
    )
    parser.add_argument(
        "--category",
        choices=["core", "expansion", "infrastructure", "vertical", "sub-vertical", "regional"],
        help="Filter by category (for available-modules)"
    )
    parser.add_argument(
        "--search",
        help="Search query (for search-modules, available-modules)"
    )
    parser.add_argument(
        "--refresh", action="store_true", default=False,
        help="Force fresh fetch of module registry from GitHub (for available-modules, search-modules)"
    )
    parser.add_argument(
        "--force", action="store_true", default=False,
        help="Force foundation sync regardless of cache freshness"
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Report drift without making changes"
    )
    parser.add_argument(
        "--user-confirmed", action="store_true", default=False,
        help="Per-invocation confirmation flag for high-impact actions"
    )
    parser.add_argument(
        "--unsafe-trust-bundled", action="store_true", default=False,
        help="Skip signature verification (emergency recovery only)"
    )

    args, _unknown = parser.parse_known_args()
    action_fn = ACTIONS.get(args.action)
    if not action_fn:
        err(f"Unknown action: {args.action}")

    try:
        action_fn(args)
    except SystemExit:
        raise
    except Exception as e:
        err(f"Unexpected error in {args.action}: {e}")


if __name__ == "__main__":
    main()
