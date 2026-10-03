"""Discovery: enumerate foundation actions + their metadata for the meta-tools.

ADR-0024 §5 makes SKILL.md / the action ACTIONS dicts the discovery source,
tied to the L0 ``test_skillmd_action_completeness`` invariant. To avoid a second
parser that can drift from that gate (SIM-0c), this module extracts action names
the *same* way the L0 gate does — AST over the ``ACTIONS = {...}`` /
``<DOMAIN>_ACTIONS = {...}`` dict literals in the foundation scripts. Descriptions
come from the SKILL.md catalog tables; the destructive flag comes from the
router's own ``DANGEROUS_ACTIONS`` frozenset (the single source of truth the
router gate uses), so the MCP layer can never disagree with the router about
what is gated.

v1 scope is foundation-only (Nik D3). The public surface accepts a ``module``
argument and ignores anything but foundation for now; all-module aggregation
across ``module_registry.json`` is later config, not a redesign.
"""
import ast
import os
import re
from functools import lru_cache

# The foundation source root: this file lives at source/erpclaw/mcp/, so the
# foundation module dir is its parent.
_FOUNDATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_FOUNDATION_DIR, "scripts")
_ROUTER_PATH = os.path.join(_SCRIPTS_DIR, "db_query.py")
_SOURCE_SKILL_MD = os.path.join(_FOUNDATION_DIR, "SKILL.md")


class RouterGateUnavailable(RuntimeError):
    """The router's DANGEROUS_ACTIONS gate could not be read or parsed.

    Fail-closed signal: callers must refuse every action with a structured
    ``destructive_gate_unavailable`` error rather than treating every action
    as non-destructive.
    """

    def __init__(self, path, reason):
        self.path = path
        self.reason = reason
        super().__init__("destructive gate unavailable at %r: %s" % (path, reason))


def _is_router_target(t):
    return isinstance(t, ast.Name) and (t.id == "ACTIONS" or t.id.endswith("_ACTIONS"))


_DISCOVERY_PROBLEMS = []


def discovery_problems():
    """Return the (path, reason) pairs for sibling scripts skipped as unparseable.

    Populated by the most recent uncached ``_foundation_action_names()`` run.
    Empty when every sibling script parsed cleanly.
    """
    return list(_DISCOVERY_PROBLEMS)


@lru_cache(maxsize=1)
def _foundation_action_names() -> frozenset:
    """AST-extract every action key from the foundation's own ACTIONS dicts.

    Mirrors testing/unit/constitution/test_skillmd_completeness._extract_python_actions
    so discovery == the L0 completeness set. Sub-module dirs that have their own
    SKILL.md own their own actions and are excluded (foundation scope, Nik D3).
    Unparseable sibling scripts are recorded in _DISCOVERY_PROBLEMS instead of
    being silently skipped.
    """
    # Sub-modules under source/ with their own SKILL.md own their own actions.
    submodule_dirs = set()
    for root, dirs, files in os.walk(_SCRIPTS_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__" and d != "tests"]
        # A scripts subtree that is itself a module (has SKILL.md alongside a
        # scripts/ dir) is excluded — but foundation domain dirs do NOT have
        # their own SKILL.md, so this is a no-op for the foundation tree and a
        # safety net if that ever changes.
        if "SKILL.md" in files and root != _FOUNDATION_DIR:
            submodule_dirs.add(os.path.abspath(root))

    _DISCOVERY_PROBLEMS.clear()
    actions = set()
    for root, dirs, files in os.walk(_SCRIPTS_DIR):
        dirs[:] = [
            d for d in dirs
            if d != "__pycache__" and d != "tests"
            and os.path.abspath(os.path.join(root, d)) not in submodule_dirs
        ]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            try:
                tree = ast.parse(open(path).read())
            except Exception as e:
                _DISCOVERY_PROBLEMS.append((path, "%s: %s" % (type(e).__name__, e)))
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Assign)
                        and any(_is_router_target(t) for t in node.targets)
                        and isinstance(node.value, ast.Dict)):
                    for k in node.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            actions.add(k.value)
    return frozenset(actions)


@lru_cache(maxsize=1)
def dangerous_actions() -> frozenset:
    """The router's DANGEROUS_ACTIONS frozenset, AST-parsed (single source of truth).

    The MCP confirm mapping reads THIS, never a copy, so the protocol layer and
    the router gate can never disagree (ADR-0024 sub-decision 2).

    Raises RouterGateUnavailable on any read/parse failure, when no
    DANGEROUS_ACTIONS assignment is found, or when the extracted set is empty
    (fail closed, never fail open to an empty set). The raise happens before
    anything is memoized: lru_cache only stores returned values, so a failure
    is never cached and a later call re-reads the router.
    """
    try:
        text = open(_ROUTER_PATH).read()
    except Exception as e:
        raise RouterGateUnavailable(_ROUTER_PATH, "%s: %s" % (type(e).__name__, e))
    try:
        tree = ast.parse(text)
    except Exception as e:
        raise RouterGateUnavailable(_ROUTER_PATH, "%s: %s" % (type(e).__name__, e))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "DANGEROUS_ACTIONS"
                        for t in node.targets)):
            names = set()
            for e in ast.walk(node.value):
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    names.add(e.value)
            if not names:
                raise RouterGateUnavailable(
                    _ROUTER_PATH, "DANGEROUS_ACTIONS assignment found but extracted set is empty")
            return frozenset(names)
    raise RouterGateUnavailable(
        _ROUTER_PATH, "DANGEROUS_ACTIONS assignment not found in router")


@lru_cache(maxsize=1)
def router_aliases() -> dict:
    """Alias name -> resolved target action, AST-parsed from the router.

    Reads the router's ``ALIASES`` dict (``alias: (domain, target)``). Raises
    RouterGateUnavailable on any read/parse failure, on a missing ALIASES
    assignment, on an empty map, or on any malformed entry (non-string key,
    non-tuple value, wrong arity, non-string members). Fail-closed: callers
    must refuse execution rather than fall back to unrestricted dispatch.
    The raise happens before memoization so a failure is never cached.
    """
    try:
        text = open(_ROUTER_PATH).read()
    except Exception as e:
        raise RouterGateUnavailable(_ROUTER_PATH, "%s: %s" % (type(e).__name__, e))
    try:
        tree = ast.parse(text)
    except Exception as e:
        raise RouterGateUnavailable(_ROUTER_PATH, "%s: %s" % (type(e).__name__, e))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(tg, ast.Name) and tg.id == "ALIASES"
                        for tg in node.targets)):
            if not isinstance(node.value, ast.Dict):
                raise RouterGateUnavailable(
                    _ROUTER_PATH, "ALIASES assignment is not a dict")
            out = {}
            for k, v in zip(node.value.keys, node.value.values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    raise RouterGateUnavailable(
                        _ROUTER_PATH, "ALIASES has a non-string key")
                if not (isinstance(v, ast.Tuple) and len(v.elts) == 2):
                    raise RouterGateUnavailable(
                        _ROUTER_PATH,
                        "ALIASES entry for %r is malformed (want (domain, action))" % (k.value,))
                dom, tgt = v.elts
                if not (isinstance(dom, ast.Constant) and isinstance(dom.value, str)
                        and isinstance(tgt, ast.Constant) and isinstance(tgt.value, str)):
                    raise RouterGateUnavailable(
                        _ROUTER_PATH,
                        "ALIASES entry for %r has non-string members" % (k.value,))
                if not dom.value or not tgt.value:
                    raise RouterGateUnavailable(
                        _ROUTER_PATH,
                        "ALIASES entry for %r has an empty member" % (k.value,))
                out[k.value] = tgt.value
            if not out:
                raise RouterGateUnavailable(
                    _ROUTER_PATH, "ALIASES assignment found but extracted map is empty")
            return dict(out)
    raise RouterGateUnavailable(
        _ROUTER_PATH, "ALIASES assignment not found in router")


def resolve_alias_target(alias: str, aliases: dict) -> str:
    """Resolve an alias through the alias map with cycle/missing checks.

    Follows chains where a target is itself an alias key. Raises
    RouterGateUnavailable on missing targets, cycles, empty hops, or
    non-string entries. Returns the final non-alias target action name.
    """
    seen = set()
    cur = alias
    while cur in aliases:
        if cur in seen:
            raise RouterGateUnavailable(
                _ROUTER_PATH, "alias cycle detected at %r" % (cur,))
        seen.add(cur)
        nxt = aliases[cur]
        if not isinstance(nxt, str) or not nxt:
            raise RouterGateUnavailable(
                _ROUTER_PATH, "alias target for %r is malformed" % (cur,))
        if len(seen) > 64:
            raise RouterGateUnavailable(
                _ROUTER_PATH, "alias chain too deep at %r" % (cur,))
        cur = nxt
    return cur


@lru_cache(maxsize=1)
def _skillmd_descriptions() -> dict:
    """Map action name → its SKILL.md catalog-row description (best effort).

    The catalog rows are markdown tables ``| `a` / `b` / `c` | description |``.
    Every backtick action token on a row shares that row's description. Used for
    human-readable tool descriptions; absence is non-fatal (name still listed).
    """
    text = _read_source_skill_md()
    descriptions = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        action_cell, desc_cell = cells[0], cells[1]
        tokens = re.findall(r"`([a-z][\w-]*)`", action_cell)
        if not tokens:
            continue
        for tok in tokens:
            descriptions[tok] = desc_cell
    return descriptions


def _read_source_skill_md() -> str:
    if os.path.isfile(_SOURCE_SKILL_MD):
        return open(_SOURCE_SKILL_MD).read()
    return ""


def _gate_error(exc):
    return {
        "status": "error",
        "error": "destructive_gate_unavailable",
        "detail": exc.reason,
        "path": exc.path,
    }


def list_actions(module: str = "foundation"):
    """Return the discoverable action catalog (foundation scope, Nik D3).

    ``module`` is accepted for forward-compat module-agnosticism; v1 serves the
    foundation catalog for any value. Each entry: ``name``, ``destructive``,
    ``description`` (may be empty). Credential carve-out actions are excluded
    here so they are not even discoverable over MCP in v1 (ADR-0024 §4).

    Fail-closed: when the router gate cannot be read, returns the structured
    ``destructive_gate_unavailable`` error dict (no partial catalog) instead of
    a list. The server envelope adds ``warnings`` from ``discovery_problems()``
    when sibling scripts were skipped.
    """
    from .confirm import CREDENTIAL_CARVE_OUT  # local import avoids a cycle

    try:
        dangerous = dangerous_actions()
    except RouterGateUnavailable as exc:
        return _gate_error(exc)
    names = _foundation_action_names()
    descs = _skillmd_descriptions()
    out = []
    for name in sorted(names):
        if name in CREDENTIAL_CARVE_OUT:
            continue
        out.append({
            "name": name,
            "destructive": name in dangerous,
            "description": descs.get(name, ""),
        })
    return out


def describe_action(action_name: str) -> dict:
    """Return the metadata payload for one action, or an error dict if unknown.

    Includes the destructive flag and the SKILL.md description. Destructive
    actions carry an explicit note that a genuine second confirmation
    (``user_confirmed: true``) is required (ADR-0024 sub-decision 2).

    Fail-closed: when the router gate cannot be read, returns the structured
    ``destructive_gate_unavailable`` error dict.
    """
    from .confirm import CREDENTIAL_CARVE_OUT

    if not isinstance(action_name, str):
        return {
            "status": "error",
            "error": "invalid_action",
            "detail": "action_name must be a string.",
        }
    try:
        carved = action_name in CREDENTIAL_CARVE_OUT
    except TypeError:
        return {
            "status": "error",
            "error": "invalid_action",
            "detail": "action_name must be a string.",
        }
    if carved:
        return {
            "status": "error",
            "error": f"Action '{action_name}' is not exposed over MCP in v1 "
                     f"(credential carve-out, ADR-0017 S0c).",
        }
    try:
        dangerous = dangerous_actions()
    except RouterGateUnavailable as exc:
        return _gate_error(exc)
    names = _foundation_action_names()
    if action_name not in names:
        return {
            "status": "error",
            "error": f"Unknown foundation action: {action_name!r}. "
                     f"Call erpclaw_list_actions to see the catalog.",
        }
    destructive = action_name in dangerous
    payload = {
        "status": "ok",
        "name": action_name,
        "destructive": destructive,
        "description": _skillmd_descriptions().get(action_name, ""),
        "args_hint": "Pass action arguments as a JSON object on erpclaw_action; "
                     "keys map to the router's --kebab-case flags "
                     "(e.g. {\"name\": \"Acme\", \"company_id\": \"...\"} "
                     "→ --name Acme --company-id ...).",
    }
    if destructive:
        payload["requires_confirmation"] = True
        payload["confirmation_note"] = (
            "This action is destructive/high-impact. erpclaw_action will NOT "
            "execute it unless called with user_confirmed=true reflecting a "
            "genuine user confirmation (ADR-0018 / ADR-0024)."
        )
    return payload
