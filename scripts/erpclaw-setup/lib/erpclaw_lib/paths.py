"""Install-path resolution for ERPClaw (the single ERPCLAW_HOME point of truth).

ERPClaw is runtime-portable from one source tree: it runs on OpenClaw (the
primary runtime) and, experimentally, on the Hermes Agent runtime. The only
thing that differs between runtimes is *where the install lives* on disk. This
module is the one place that resolves that location, via the canonical
``ERPCLAW_HOME`` environment variable (ADR-0017).

``ERPCLAW_HOME`` is a *defaulting* variable, not a mandatory one. When it is
unset, every resolver here returns the historical ``~/.openclaw/erpclaw`` paths
**byte-for-byte**, so existing OpenClaw installs see zero behavior change. This
backward-compat contract is load-bearing; the L0 selftest
(``testing/unit/L0/test_paths_resolution.py``) guards it.

Hermes sets ``ERPCLAW_HOME`` to its skill-dir-derived path, redirecting the lib,
DB, modules, and install-state locations together. There is exactly one var,
resolved once, consumed everywhere; no per-module env var is permitted.

This module imports only ``os`` (stdlib). It has zero intra-lib imports so it is
safe to import from anywhere in the lib, including ``db.py`` (``db.py`` imports
``paths``, never the reverse — no cycle).
"""
import os

# The historical OpenClaw install root. Used as the ERPCLAW_HOME default so an
# unset environment resolves to exactly today's paths.
_DEFAULT_HOME = "~/.openclaw/erpclaw"


def erpclaw_home() -> str:
    """Resolve the ERPClaw install root.

    Returns ``$ERPCLAW_HOME`` (expanduser-normalized so the var may itself
    contain ``~``) when set, else ``~/.openclaw/erpclaw`` expanded. With
    ERPCLAW_HOME unset this equals ``os.path.expanduser("~/.openclaw/erpclaw")``
    byte-for-byte — the ADR-0017 backward-compat contract.
    """
    return os.path.expanduser(os.environ.get("ERPCLAW_HOME", _DEFAULT_HOME))


def lib_dir() -> str:
    """Path to the shared lib dir (the ``sys.path.insert`` target)."""
    return os.path.join(erpclaw_home(), "lib")


def db_default() -> str:
    """Default SQLite DB path.

    This is only the *default*; the ``ERPCLAW_DB_URL`` / ``ERPCLAW_DB_PATH``
    chain in ``db.py`` stays the authority for the DB location specifically.
    ``ERPCLAW_HOME`` supplies the SQLite default underneath it.
    """
    return os.path.join(erpclaw_home(), "data.sqlite")


def modules_dir() -> str:
    """Path to the installed-expansion-modules dir."""
    return os.path.join(erpclaw_home(), "modules")


def install_state_dir() -> str:
    """Path holding foundation install-state markers.

    The module manager's foundation state files (``registry_cache.json``,
    ``.sync.lock``, ``.no_autosync``, ``.last_registry_version``,
    ``logs/sync.log``) all sit directly under the install root, so this is the
    install root itself.
    """
    return erpclaw_home()
