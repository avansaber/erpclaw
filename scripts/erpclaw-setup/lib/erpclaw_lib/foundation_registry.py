"""Foundation catalog-row provisioning (M39 / Wave G F6).

``initialize-database`` created every table but never INSERTed the foundation's
own ``erpclaw_module`` row, so a ClawHub install — whose post hook runs
``initialize-database`` and nothing else — was never catalogued. Three shipped
readers then lied: ``update-modules`` answered "No modules to update",
``list-modules`` came back empty, and ``install-module``'s dependency resolver
saw ``erpclaw`` as missing and drove the addon git-clone installer against the
foundation. ADR-0028 §2's ``_bump_foundation_version_row`` heal could not repair
any of it: it is an UPDATE, and there was no row to update.

This module is the one place that decides what the foundation's catalog row
contains. Both writers use it — ``initialize-database`` (fresh install) and
``update-foundation`` (the reconcile/heal path) — so a row created by either is
identical, and a DB that missed the fresh INSERT heals on the next reconcile.

De-counting rule (2026-07-27): every seeded value is READ from
``module_registry.json`` at runtime. No version literal, no hand-typed
``display_name``/``category``, and ``action_count`` is deliberately left at the
DDL default — ``rebuild-action-cache`` derives it from a real scan of the
installed tree, and ``list-modules`` reports the ``erpclaw_module_action`` COUNT
as the authoritative figure. A second hand-maintained count in a DB row is the
liability that ruling names.

Stdlib-only (``json``/``os``/``uuid``) plus the caller's connection, so it is
importable from the setup skill, the module manager, and tests alike.
"""
import json
import os
import uuid
from datetime import datetime, timezone

# The foundation's own name in module_registry.json and in erpclaw_module.name.
FOUNDATION_MODULE_NAME = "erpclaw"


def default_registry_path() -> str:
    """Path to the bundled ``module_registry.json``, derived from this file.

    This module ships at ``<skill>/scripts/erpclaw-setup/lib/erpclaw_lib/``, so
    the registry is three levels up. ``realpath`` first: the installed lib is
    reached through the ``ERPCLAW_HOME/lib`` symlink that ``_link_shared_library``
    creates, and walking up from the symlinked path would land in the install
    root instead of the skill tree.
    """
    here = os.path.dirname(os.path.realpath(__file__))          # …/lib/erpclaw_lib
    scripts_dir = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return os.path.join(scripts_dir, "module_registry.json")


def load_foundation_entry(registry_path=None):
    """Return the ``erpclaw`` entry from ``module_registry.json``, or None.

    Never raises: a missing or unparseable registry means the catalog row cannot
    be seeded honestly, and the caller reports that instead of inventing values.
    Handles both registry shapes the manager supports (name-keyed dict and list).
    """
    path = registry_path or default_registry_path()
    try:
        with open(path, "r") as f:
            registry = json.load(f)
    except (OSError, ValueError):
        return None

    modules = registry.get("modules", {})
    if isinstance(modules, dict):
        entry = modules.get(FOUNDATION_MODULE_NAME)
    else:
        entry = next((m for m in modules
                      if m.get("name") == FOUNDATION_MODULE_NAME), None)
    if not entry:
        return None
    entry = dict(entry)
    entry.setdefault("name", FOUNDATION_MODULE_NAME)
    return entry


def _row_present(conn) -> bool:
    row = conn.execute(
        "SELECT id FROM erpclaw_module WHERE name = ?",
        (FOUNDATION_MODULE_NAME,),
    ).fetchone()
    return row is not None


def ensure_foundation_module_row(conn, registry_entry, install_path=None) -> dict:
    """INSERT the foundation's ``erpclaw_module`` row when it is absent.

    Insert-if-absent by design, not an upsert: an existing row's ``version`` is
    ADR-0028's ``_bump_foundation_version_row`` to heal (it runs right after this
    on the reconcile path), and this helper must stay a pure creator so that
    contract — and its "bump-in-isolation is a clean no-op" test — is untouched.

    Idempotent: a second call on a DB that already carries the row writes
    nothing and reports ``inserted: False``. ``name`` is UNIQUE, so a lost race
    surfaces as an IntegrityError, which is re-checked rather than re-raised.

    Returns a JSON-safe dict for the caller to surface; never raises on a
    missing/unusable registry entry — it reports why instead.
    """
    if not registry_entry:
        return {"ensured": False, "inserted": False,
                "reason": "registry has no erpclaw foundation entry"}

    if _row_present(conn):
        return {"ensured": True, "inserted": False, "reason": "row already present"}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = (
        str(uuid.uuid4()),
        FOUNDATION_MODULE_NAME,
        registry_entry.get("display_name", FOUNDATION_MODULE_NAME),
        registry_entry.get("version", "0.0.0"),
        registry_entry.get("category", "core"),
        registry_entry.get("github", registry_entry.get("github_repo", "")),
        install_path or "",
        now,
        now,
        json.dumps(registry_entry.get("requires", [])),
    )
    try:
        conn.execute(
            """INSERT INTO erpclaw_module
               (id, name, display_name, version, category, github_repo,
                install_path, installed_at, updated_at, install_status,
                is_active, requires_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'installed', 1, ?)""",
            values,
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001 — UNIQUE race or a rejected CHECK value
        if _row_present(conn):
            return {"ensured": True, "inserted": False,
                    "reason": "row inserted concurrently"}
        return {"ensured": False, "inserted": False, "reason": str(e)}

    return {"ensured": True, "inserted": True,
            "name": FOUNDATION_MODULE_NAME, "version": values[3]}
