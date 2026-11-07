"""Plugin loader, registry, slots and per-books installs.

Plugins are found through the `erpclaw.plugins` entry-point group. Each one
exposes a PLUGIN object: its manifest (`manifest.py`) plus the contracts,
metrics, slot callbacks and services the manifest declares. Only first-party,
in-process (tier 1) plugins load; this is not a sandbox. The loader fails
closed: the first plugin that breaks a rule stops the engine from starting,
with the rule and the fix. There is no "load what you can" mode.
"""
from __future__ import annotations

import importlib
import types
import typing
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import entry_points, version
from decimal import Decimal
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version
from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection, Engine

from . import approval_scope
from .common import Ctx
from .contracts import Contract, Money, Service, WriteContract, no_date_reason_problem
from .manifest import (EFFECTS, FIRST_PARTY_DISTRIBUTIONS, PUBLISHER, SERVICE_EFFECTS, Manifest, artifact_digest,
                       parse_use)

# The plugin interface this engine offers: the manifest schema (with services and uses), the loader's refusals,
# Contract's fields, the service declaration and registration, call_service and Ctx.register_check. Its contract
# fixtures are in the engine's tests/contracts/plugin/v1/; a change that breaks a plugin is a new version.
PLUGIN_CONTRACT = "plugin/1"
GROUP = "erpclaw.plugins"
MESSAGE_SLOTS = {"deliver_message", "send_message"}
# Core's slots and their cardinality; a plugin fills only these.
SLOTS = {"deliver_message": "one",
         # plugin/2: a mail connector that sends under core's data mark, and the company's sender
         "send_message": "one", "message_sender": "one",
         # connector slots of the integration kernel: one accounting and one
         # payroll system per set of books; masters and journals may come from several
         "post_journal": "one", "import_journal": "many", "sync_parties": "many", "sync_items": "many",
         "import_payroll": "one", "fetch_balances": "one"}
SLOT_WORDS = {"deliver_message": "delivers messages", "send_message": "sends email",
              "message_sender": "gives the company's email sender", "post_journal": "posts journals to an external system",
              "import_journal": "imports journals", "sync_parties": "syncs customers and suppliers",
              "sync_items": "syncs items", "import_payroll": "imports payroll",
              "fetch_balances": "fetches balances from an external system"}


class PluginRefused(RuntimeError):
    """A plugin broke a loading rule; the engine does not start."""


class EmptySlot(LookupError):
    """No enabled plugin fills the slot in these books."""


class BooksRefused(RuntimeError):
    """The books' plugin installs do not match this engine."""


@dataclass
class Plugin:
    manifest: Manifest
    kind: str  # "core" | "business-module" | "connector" | "country-pack"
    contracts: list[Contract] = field(default_factory=list)
    metrics: list = field(default_factory=list)
    slots: dict[str, Callable] = field(default_factory=dict)
    artifact_digest: str | None = None  # set by the loader from the files on disk
    migrations: Path | None = None  # its branch's versions folder, set by the loader
    services: list[Service] = field(default_factory=list)  # registered for the manifest's `services`

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version


class Registry:
    def __init__(self):
        self.plugins: dict[str, Plugin] = {}
        self.order: list[str] = []
        self.contracts: dict[str, Contract] = {}
        self.metrics: dict = {}
        self.slots: dict[str, dict[str, Callable]] = {}  # slot -> {plugin name: callback}
        self.contract_plugin: dict[str, str] = {}
        self.metric_plugin: dict[str, str] = {}
        self.services: dict[str, Service] = {}  # "<plugin>.<service>" -> its registration

    def fill(self, plugins: list[Plugin]) -> None:
        """Validate each plugin and the whole set, then register it in dependency order."""
        for p in plugins:
            _check_plugin(p)
        by_name = _check_set(plugins)
        self.__init__()
        for name in _dependency_order(by_name):
            p = by_name[name]
            self.plugins[name] = p
            self.order.append(name)
            for c in p.contracts:
                # Core registration covers reads/previews dispatched by the assistant
                # without changing its paths or any frozen Contract fields.
                for callback in ("run", "preview", "commit", "read_set", "find_by_operation_key"):
                    fn = getattr(c, callback)
                    if fn is not None:
                        setattr(c, callback, approval_scope._wrap_contract(fn, name, c.name, callback))
                self.contracts[c.name] = c
                self.contract_plugin[c.name] = name
            for mt in p.metrics:
                mt.run = approval_scope._wrap_metric(mt.run, name)
                self.metrics[mt.name] = mt
                self.metric_plugin[mt.name] = name
            for slot, fn in p.slots.items():
                self.slots.setdefault(slot, {})[name] = fn
            for sv in p.services:
                self.services[f"{name}.{sv.name}"] = sv

    def slot_filler(self, slot: str, enabled: set[str]) -> tuple[str, Callable]:
        fillers = [(n, fn) for n, fn in self.slots.get(slot, {}).items() if n in enabled]
        if not fillers:
            raise EmptySlot(f"No plugin {SLOT_WORDS.get(slot, 'fills the ' + slot + ' slot')} in these books.")
        return fillers[0]


REGISTRY = Registry()
_loaded = False


def _refuse(plugin: str, rule: str, fix: str) -> PluginRefused:
    return PluginRefused(f"plugin {plugin}: {rule}. {fix}")


def _engine_version() -> Version:
    return Version(version("erpclaw-core"))


def _core_revisions() -> set[str]:
    from .migrate import core_revisions

    return set(core_revisions())


def _check_branch(p: Plugin) -> None:
    """A plugin with tables ships its own branch: one base labelled with its name,
    no depends_on, and its revisions together create exactly its owned tables,
    each once (each revision declares `creates`)."""
    from .migrate import branch_scripts

    name, owned = p.name, list(p.manifest.owned_tables)
    if p.migrations is None:
        if owned:
            raise _refuse(name, "owns tables but ships no migrations", "Add migrations/versions/ with its branch.")
        return
    outside = "A plugin's branch has its own base and no depends_on; the engine checks the core order."
    try:  # read alone: a revision naming one outside the branch does not resolve
        scripts = branch_scripts(p.migrations)
    except Exception as e:  # noqa: BLE001  (any unreadable branch is refused, never loaded)
        raise _refuse(name, f"its migrations reach outside its branch or cannot be read ({type(e).__name__}: {e})",
                      outside) from e
    ids = {sc.revision for sc in scripts}
    bases = [sc for sc in scripts if sc.down_revision is None]
    if len(bases) != 1 or set(bases[0].branch_labels) != {name}:
        raise _refuse(name, "its migrations need exactly one base, labelled with the plugin's name",
                      f"Give the first revision down_revision = None and branch_labels = ('{name}',).")
    created: list[str] = []
    for sc in scripts:
        downs = sc.down_revision if isinstance(sc.down_revision, tuple) else (sc.down_revision,)
        if sc.dependencies or any(d is not None and d not in ids for d in downs):
            raise _refuse(name, f"revision {sc.revision} reaches outside its branch", outside)
        creates = getattr(sc.module, "creates", None)
        if not isinstance(creates, tuple):
            raise _refuse(name, f"revision {sc.revision} does not declare the tables it creates",
                          "Set creates = (...) in the revision, () if none.")
        created += list(creates)
    twice = sorted({t for t in created if created.count(t) > 1})
    if twice:
        raise _refuse(name, f"table {twice[0]} is created by more than one of its revisions", "Create each once.")
    if set(created) != set(owned):
        missing, extra = sorted(set(owned) - set(created)), sorted(set(created) - set(owned))
        raise _refuse(name, f"owns table {missing[0]}, which none of its revisions creates" if missing else
                      f"a revision creates table {extra[0]}, which it does not own",
                      "Make its revisions create exactly its owned tables.")


def _check_plugin(p: Plugin) -> None:
    m, name = p.manifest, p.manifest.name
    if _engine_version() not in SpecifierSet(m.engine):
        raise _refuse(name, f"needs engine {m.engine}, this engine is {_engine_version()}", "Install a matching version.")
    if m.after_core_revision not in _core_revisions():
        raise _refuse(name, f"needs core revision {m.after_core_revision}, which this engine does not have",
                      "Install a matching engine.")
    declared = {c.name: c.effect for c in p.contracts}
    for c in p.contracts:
        if c.effect not in EFFECTS:
            raise _refuse(name, f"contract {c.name} has effect {c.effect!r}, not one of {', '.join(EFFECTS)}",
                          "Classify every operation.")
        if c.plugin != name:
            raise _refuse(name, f"contract {c.name} claims plugin {c.plugin}", "Fix the contract's plugin.")
        if c.effect in ("write", "destructive") and not (c.preview and c.commit and c.find_by_operation_key
                                                          and c.read_set):
            raise _refuse(name, f"{c.effect} contract {c.name} needs preview, commit, operation-key read-back and "
                                "a read set", "Add them.")
        if c.effect == "draft" and not (c.preview and c.commit):
            raise _refuse(name, f"draft contract {c.name} needs preview and commit", "Add them.")
        if c.effect == "read" and not c.run:
            raise _refuse(name, f"read contract {c.name} needs run", "Add it.")
        if isinstance(c, WriteContract) and c.effect == "read":
            raise _refuse(name, f"read contract {c.name} is built as a WriteContract", "Build it as a Contract.")
        if problem := no_date_reason_problem(c):
            raise _refuse(name, f"write {c.name} {problem}", "Declare one sentence per language on its contract.")
    if len(declared) != len(p.contracts) or declared != dict(m.contracts):
        raise _refuse(name, "its contracts differ from its manifest", "Make the manifest list exactly its contracts.")
    metric_names = [mt.name for mt in p.metrics]
    if len(set(metric_names)) != len(metric_names) or set(metric_names) != set(m.metrics) \
            or len(set(m.metrics)) != len(m.metrics):
        raise _refuse(name, "its metrics differ from its manifest", "Make the manifest list exactly its metrics.")
    _check_services(p)
    if set(p.slots) != set(m.slots):
        raise _refuse(name, "its slot callbacks differ from its manifest", "Make the manifest list exactly its slots.")
    for slot, decl in m.slots.items():
        if slot not in SLOTS:
            raise _refuse(name, f"fills unknown slot {slot}", f"Core's slots are {', '.join(SLOTS)}.")
        if decl.cardinality != SLOTS[slot]:
            raise _refuse(name, f"declares slot {slot} as {decl.cardinality!r}, core defines it as {SLOTS[slot]!r}",
                          "Use core's cardinality.")


def _models_in(annotation) -> list[type[BaseModel]]:
    """The Pydantic models an annotation names, through list, dict, Optional, Annotated and unions."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    if typing.get_origin(annotation) is None and not isinstance(annotation, types.UnionType):
        return []
    return [mdl for arg in typing.get_args(annotation) for mdl in _models_in(arg)]


def _open_model(model: type[BaseModel], seen: set | None = None) -> type[BaseModel] | None:
    """The first model, this one or one nested in its fields, that does not refuse unknown fields."""
    seen = set() if seen is None else seen
    if model in seen:
        return None
    seen.add(model)
    if model.model_config.get("extra") != "forbid":
        return model
    for f in model.model_fields.values():
        for inner in _models_in(f.annotation):
            found = _open_model(inner, seen)
            if found is not None:
                return found
    return None


def _check_services(p: Plugin) -> None:
    """Every declared service is registered once, with the declared effect, Pydantic input and output models that
    refuse unknown fields, and a function; nothing is registered that the manifest does not declare."""
    name = p.name
    declared = {d.name: d.effect for d in p.manifest.services}
    registered: dict[str, Service] = {}
    for sv in p.services:
        if not isinstance(sv, Service):
            raise _refuse(name, f"registers {sv!r} as a service, which is not a Service", "Register a Service.")
        if sv.name in registered:
            raise _refuse(name, f"registers service {sv.name} twice", "Register each service once.")
        registered[sv.name] = sv
        if sv.name not in declared:
            raise _refuse(name, f"registers service {sv.name}, which its manifest does not declare",
                          "Declare it in the manifest's services, or remove the registration.")
        if sv.effect not in SERVICE_EFFECTS or sv.effect != declared[sv.name]:
            raise _refuse(name, f"service {sv.name} is registered as {sv.effect!r}, its manifest declares "
                                f"{declared[sv.name]!r}", "Make the registration and the manifest agree.")
        for role, model in (("input", sv.input_model), ("output", sv.output_model)):
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                raise _refuse(name, f"service {sv.name} has no Pydantic {role} model", "Register its models.")
            open_model = _open_model(model)
            if open_model is not None:
                raise _refuse(name, f"service {sv.name}'s {role} model {open_model.__name__} accepts unknown fields",
                              "Base it on ServiceModel, or set extra='forbid'.")
        if not callable(sv.fn):
            raise _refuse(name, f"service {sv.name} has no function", "Register its function.")
    for svc in declared:
        if svc not in registered:
            raise _refuse(name, f"declares service {svc}, which it does not register",
                          "Register it in the plugin's services, or remove it from the manifest.")


def _check_set(plugins: list[Plugin]) -> dict[str, Plugin]:
    by_name: dict[str, Plugin] = {}
    for p in plugins:
        if p.name in by_name:
            raise _refuse(p.name, "is loaded twice", "Remove the duplicate entry point.")
        by_name[p.name] = p
    _check_unique(plugins, lambda p: [c.name for c in p.contracts], "contract")
    _check_unique(plugins, lambda p: [mt.name for mt in p.metrics], "metric")
    _check_unique(plugins, lambda p: p.manifest.voucher_types, "voucher type")
    _check_unique(plugins, lambda p: p.manifest.owned_tables, "table")
    for p in plugins:
        m = p.manifest
        for dep, spec in m.depends.items():
            if dep not in by_name:
                raise _refuse(p.name, f"depends on {dep}, which is not loaded", f"Install {dep}.")
            if Version(by_name[dep].version) not in SpecifierSet(spec):
                raise _refuse(p.name, f"needs {dep} {spec}, loaded is {by_name[dep].version}", f"Install a matching {dep}.")
        for other in m.conflicts:
            if other in by_name:
                raise _refuse(p.name, f"conflicts with {other}, which is loaded", f"Remove one of {p.name} and {other}.")
        for use, optional in map(parse_use, m.uses):
            dep, _, svc = use.partition(".")
            if optional:  # checked only when its plugin is loaded; never a dependency
                if dep in m.depends:
                    raise _refuse(p.name, f"{dep} is both a dependency and an optional use",
                                  f"List {dep} in one of them.")
                if dep in by_name and svc not in {d.name for d in by_name[dep].manifest.services}:
                    raise _refuse(p.name, f"optionally uses {use}, which {dep} does not declare",
                                  f"Use a service {dep} declares.")
                continue
            if dep not in m.depends:
                raise _refuse(p.name, f"uses {use} but does not depend on {dep}", f"Add {dep} to its depends.")
            if svc not in {d.name for d in by_name[dep].manifest.services}:
                raise _refuse(p.name, f"uses {use}, which {dep} does not declare", f"Use a service {dep} declares.")
        reachable = {d for d in m.depends}
        callable_contracts = {c.name for d in reachable for c in by_name[d].contracts}
        for call in m.calls:
            if call not in callable_contracts:
                raise _refuse(p.name, f"calls {call}, which no declared dependency provides", "Declare the dependency.")
        emitted = {e for d in reachable for e in by_name[d].manifest.events.emits}
        for event in m.events.consumes:
            if event not in emitted:
                raise _refuse(p.name, f"consumes event {event}, which no declared dependency emits",
                              "Declare the dependency.")
    for slot, card in SLOTS.items():
        fillers = [p.name for p in plugins if slot in p.slots]
        if card == "one" and len(fillers) > 1:
            raise _refuse(fillers[1], f"fills slot {slot}, already filled by {fillers[0]}", "Install only one.")
    _check_tables(by_name)
    for p in plugins:  # after the table marks: each plugin's branch creates exactly its owned tables
        _check_branch(p)
    _dependency_order(by_name)  # raises on a cycle
    return by_name


def _check_unique(plugins: list[Plugin], items: Callable[[Plugin], list[str]], what: str) -> None:
    seen: dict[str, str] = {}
    for p in plugins:
        for x in items(p):
            if x in seen:
                raise _refuse(p.name, f"{what} {x} is already declared by {seen[x]}", f"Each {what} has one owner.")
            seen[x] = p.name


def _check_tables(by_name: dict[str, Plugin]) -> None:
    """Every table carries its owner; each plugin's marked tables equal its declaration."""
    from .db import metadata
    from .models import CORE_TABLES

    marked: dict[str, set[str]] = {}
    for t in metadata.tables.values():
        owner = t.info.get("owner")
        if owner is None:
            raise PluginRefused(f"table {t.name} has no owner mark: every table declares core or its plugin")
        if (owner == "core") != (t.name in CORE_TABLES):
            raise PluginRefused(f"table {t.name} is marked {owner!r}, but it is not one of core's tables"
                                if owner == "core" else f"core table {t.name} is marked for plugin {owner}")
        if owner != "core" and owner not in by_name:
            raise PluginRefused(f"table {t.name} belongs to plugin {owner}, which is not loaded")
        marked.setdefault(owner, set()).add(t.name)
    for name, p in by_name.items():
        if name == "core":
            continue
        claimed = set(p.manifest.owned_tables)
        core_claimed = claimed & CORE_TABLES
        if core_claimed:
            raise _refuse(name, f"claims core table {sorted(core_claimed)[0]}", "Core tables belong to core.")
        if claimed != marked.get(name, set()):
            raise _refuse(name, f"declares tables {sorted(claimed)} but defines {sorted(marked.get(name, set()))}",
                          "Make owned_tables list exactly the tables it defines.")


def _dependency_order(by_name: dict[str, Plugin]) -> list[str]:
    order: list[str] = []
    state: dict[str, str] = {}

    def visit(name: str, path: list[str]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            cycle = path[path.index(name):] + [name]
            raise _refuse(name, f"is in a dependency cycle: {' -> '.join(cycle)}", "Break the cycle.")
        state[name] = "visiting"
        for dep in sorted(by_name[name].manifest.depends):
            visit(dep, path + [name])
        state[name] = "done"
        order.append(name)

    for name in sorted(by_name):
        visit(name, [])
    if "core" in order:
        order.remove("core")
        order.insert(0, "core")
    return order


def _load_entry_point(ep) -> Plugin:
    dist = canonicalize_name(ep.dist.name) if ep.dist is not None else None
    if dist not in FIRST_PARTY_DISTRIBUTIONS:
        raise PluginRefused(f"entry point {ep.name} comes from {dist or 'an unknown distribution'}, which is not "
                            "a first-party distribution; third-party plugins do not run in this version")
    try:
        obj = ep.load()
    except Exception as e:  # a plugin that cannot even import is refused, never skipped
        raise PluginRefused(f"entry point {ep.name} ({dist}) failed to load: {type(e).__name__}: {e}") from e
    if not isinstance(obj, Plugin) or not isinstance(getattr(obj, "manifest", None), Manifest):
        raise PluginRefused(f"entry point {ep.name} ({dist}) is not a Plugin with a manifest")
    if obj.name == "core":
        raise _refuse(obj.name, "uses the reserved name core", "Rename the plugin.")
    if obj.manifest.publisher != PUBLISHER:
        raise _refuse(obj.name, f"publisher {obj.manifest.publisher!r} is not {PUBLISHER!r}",
                      "Only first-party plugins run in this version.")
    if canonicalize_name(obj.manifest.distribution) != dist:
        raise _refuse(obj.name, f"manifest names distribution {obj.manifest.distribution}, but it comes from {dist}",
                      "Fix the manifest's distribution.")
    top = ep.module.split(".")[0]
    package = Path(importlib.import_module(top).__file__).parent
    obj.artifact_digest = artifact_digest(package)
    versions = package / "migrations" / "versions"
    obj.migrations = versions if versions.is_dir() else None
    return obj


def load_plugins() -> Registry:
    global _loaded
    if _loaded:
        return REGISTRY
    from . import core_contracts  # core's own contracts and metrics, from code

    found = [core_contracts.PLUGIN]
    for ep in sorted(entry_points(group=GROUP), key=lambda e: e.name):
        found.append(_load_entry_point(ep))
    REGISTRY.fill(found)
    _loaded = True
    return REGISTRY


# --- per set of books -------------------------------------------------------


def _install_rows(conn: Connection) -> dict[str, dict]:
    from . import models as m

    if not inspect(conn).has_table("plugin_install"):
        return {}
    return {r["name"]: dict(r) for r in conn.execute(select(m.plugin_install)).mappings()}


def enabled_plugins(conn: Connection) -> set[str]:
    """Core plus every plugin installed and enabled in these books and loaded here."""
    rows = _install_rows(conn)
    return {"core"} | {n for n, r in rows.items() if r["status"] == "installed" and n in REGISTRY.plugins}


def check_installed(engine: Engine) -> None:
    """Opening books: every installed plugin is loaded with the recorded
    version and digests, and every enabled plugin's dependencies are
    installed and enabled here. Otherwise the books do not open."""
    from .db import read_only

    with read_only(engine) as conn:
        check_books(conn)


@lru_cache(maxsize=None)
def _branch_ids(versions: Path) -> frozenset[str]:
    from .migrate import branch_scripts

    return frozenset(sc.revision for sc in branch_scripts(versions))


def check_books(conn: Connection) -> None:
    """check_installed on an open connection; lifecycle changes run it first."""
    from .migrate import applied_revisions

    present, applied = set(inspect(conn).get_table_names()), applied_revisions(conn)
    for name, p in sorted(REGISTRY.plugins.items()):
        if p.migrations is not None and present & set(p.manifest.owned_tables) and not applied & _branch_ids(p.migrations):
            raise BooksRefused(f"These books hold {name} tables with no {name} migration history: a pre-release build "
                               "made them, and they must be recreated (demo books: `init --force`).")
    rows = _install_rows(conn)
    for name, r in sorted(rows.items()):
        p = REGISTRY.plugins.get(name)
        if p is None:
            raise BooksRefused(f"These books use the plugin {name} {r['version']}, which this engine does not have.")
        differs = [what for what, rec, now in (("version", r["version"], p.version),
                                               ("manifest", r["manifest_digest"], p.manifest.digest()),
                                               ("code", r["artifact_digest"], p.artifact_digest)) if rec != now]
        if differs:
            raise BooksRefused(f"These books were set up with plugin {name} {r['version']}; this engine has "
                               f"{p.version} with a different {' and '.join(differs)}. Upgrading needs a backup "
                               "first and is not available yet; demo books can be re-created with `init --force`.")
        if r["status"] == "installed":
            for dep in p.manifest.depends:
                if rows.get(dep, {}).get("status") != "installed":
                    raise BooksRefused(f"Plugin {name} needs {dep}, which is not installed and enabled in these books.")


def _not_enabled(plugin: str, operation: str | None):
    from .common import Refusal

    return Refusal(f"The {plugin} plugin is not enabled in these books.", "plugin_not_enabled",
                   capability=f"available.op.{operation}" if operation else None)


def ensure_enabled(conn: Connection, plugin: str, operation: str | None = None) -> None:
    """Refuse unless the plugin is enabled in these books. Asked for one of its operations, the refusal names that
    operation's catalogue entry as available (in a plugin these books have not enabled)."""
    if plugin not in enabled_plugins(conn):
        raise _not_enabled(plugin, operation)


def operation_enabled(enabled: set[str], operation: str) -> None:
    """Refuse an operation whose plugin is not in `enabled` (these books' enabled_plugins, read once by the caller),
    with the same refusal as ensure_enabled."""
    plugin = REGISTRY.contract_plugin[operation]
    if plugin not in enabled:
        raise _not_enabled(plugin, operation)


PLAIN_TYPES = (dict, list, str, int, bool, type(None), Decimal)  # by exact type: no subclass is plain


def _not_plain(value, path: str = "") -> str | None:
    """Where a value is not plain JSON-shaped data; None when it is all plain. Plain is, by exact type: a dict whose
    keys are exactly text, a list, text, an integer, a bool, None and a Decimal. Anything else is not: a float, a
    tuple, a set, bytes, a model instance, an iterator, and every subclass (an enum member, an OrderedDict, a str
    or dict subclass). Nothing but type() and iteration of an exact dict or list touches the value."""
    where = path or "the value"
    kind = type(value)
    if kind is float:
        return f"{where} is a floating-point number; amounts are exact decimals"
    if kind not in PLAIN_TYPES:
        return f"{where} is a {kind.__name__}, not plain JSON-shaped data"
    if kind is dict:
        for k, v in value.items():
            if type(k) is not str:
                return f"{where} has a key that is not plain text"
            if (found := _not_plain(v, f"{path}.{k}" if path else k)) is not None:
                return found
    elif kind is list:
        for i, v in enumerate(value):
            if (found := _not_plain(v, f"{path}[{i}]")) is not None:
                return found
    return None


_MISSING = object()


def _bool_as_number(plain, validated: tuple, path: str = "") -> str | None:
    """Where the model turned a bool into a number (an int field given true or false); None if nowhere.
    `validated` is the validated model's python-mode dump by alias and by field name, walked beside the plain
    data, which may use either name."""
    by_alias, by_name = validated
    if type(plain) is bool:
        value = by_alias if by_alias is not _MISSING else by_name
        return None if type(value) is bool else f"{path or 'the value'} is true or false where a number is required"
    if type(plain) is dict:
        for k, v in plain.items():
            pair = (by_alias.get(k, _MISSING) if type(by_alias) is dict else _MISSING,
                    by_name.get(k, _MISSING) if type(by_name) is dict else _MISSING)
            if not all(x is _MISSING for x in pair) and (w := _bool_as_number(v, pair, f"{path}.{k}" if path else k)):
                return w
    if type(plain) is list:
        for i, v in enumerate(plain):
            pair = tuple(x[i] if type(x) in (list, tuple) and i < len(x) else _MISSING for x in (by_alias, by_name))
            if not all(x is _MISSING for x in pair) and (w := _bool_as_number(v, pair, f"{path}[{i}]")):
                return w
    return None


def _validation_text(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'the value'}: {err['msg']}" for err in e.errors()[:4])


def _validated(model: type[BaseModel], raw, what: str, code: str) -> BaseModel:
    """`raw` checked to be plain JSON-shaped data before anything else touches it, then validated by `model`
    (by alias, as model_validate does); a bool the model would read as a number is refused."""
    from .common import Refusal

    bad = f"it is a {type(raw).__name__}, not a dict" if type(raw) is not dict else _not_plain(raw)
    if bad is not None:
        raise Refusal(f"{what} is not valid: {bad}.", code)
    try:
        validated = model.model_validate(raw)
    except ValidationError as e:
        raise Refusal(f"{what} is not valid: {_validation_text(e)}.", code) from None
    dumps = (validated.model_dump(mode="python", by_alias=True, warnings=False),
             validated.model_dump(mode="python", warnings=False))
    if (bad := _bool_as_number(raw, dumps)) is not None:
        raise Refusal(f"{what} is not valid: {bad}.", code)
    return validated


def _currencies(value) -> set[str]:
    """The currency of every Money in a validated model, nested ones included."""
    if isinstance(value, Money):
        return {value.currency}
    if isinstance(value, BaseModel):
        return set().union(*(_currencies(getattr(value, f)) for f in type(value).model_fields))
    if isinstance(value, (list, tuple)):
        return set().union(*(_currencies(v) for v in value))
    if isinstance(value, dict):
        return set().union(*(_currencies(v) for v in value.values()))
    return set()


def _known_currencies(ctx: Ctx, model: BaseModel, what: str) -> None:
    from . import models as m
    from .common import Refusal

    codes = _currencies(model)
    known = set(ctx.conn.execute(select(m.currency.c.code).where(m.currency.c.code.in_(codes))).scalars()) \
        if codes else set()
    if codes - known:
        raise Refusal(f"{what} names a currency these books do not have: {', '.join(sorted(codes - known))}.",
                      "unknown_currency")


def declares_service(plugin: str, name: str) -> bool:
    """Whether the plugin declares the service `name` ("<plugin>.<service>", plain), as a plain use or an optional one:
    the one answer every reader uses, so no plugin re-derives it."""
    p = REGISTRY.plugins.get(plugin)
    return p is not None and any(parse_use(u)[0] == name for u in p.manifest.uses)


def call_service(ctx: Ctx, caller: str, name: str, input: dict) -> dict:
    """Call another plugin's service on the caller's `Ctx`: the same connection, transaction and company. Only plain
    JSON-shaped data crosses, both ways (_not_plain): the caller passes a dict, validated into the service's input
    model; the service returns a dict, never a model instance, validated by its output model; the caller receives
    that model's model_dump(mode="json", by_alias=True), checked to be plain again, since the dump runs the model's
    own serializers. Refused (each a Refusal, nothing written by the call): a service the caller's manifest does
    not list in `uses`, plain or optional (undeclared_service); a callee not enabled in these books, or an optional
    one not loaded here (plugin_not_enabled); a write
    service on any `Ctx` but an approved card's commit (service_outside_commit); an input or an output that is not
    plain data or does not match its model (service_input, service_output); a `Money` in either whose currency is
    not one of the books' (unknown_currency). A Refusal the service raises reaches
    the caller unchanged: its code, message, options and data."""
    from .common import Refusal

    p = REGISTRY.plugins.get(caller)
    sv = REGISTRY.services.get(name)
    callee = name.partition(".")[0]
    optional = p is not None and any(parse_use(u) == (name, True) for u in p.manifest.uses)
    owner = approval_scope._caller_owner(ctx)
    if (not declares_service(caller, name) or (sv is None and not optional)
            or (owner is not None and owner != caller)):
        raise Refusal(f"The {caller} plugin calls the service {name}, which it does not declare.",
                      "undeclared_service")
    if sv is None:  # an optional service whose plugin is not loaded here: as disabled, never a silent no-op
        raise Refusal(f"The {callee} plugin is not enabled in these books.", "plugin_not_enabled")
    ensure_enabled(ctx.conn, callee)
    if sv.effect == "write" and approval_scope.committing(ctx) is None:
        raise Refusal(f"The service {name} writes, so it runs only inside an approved card's commit.",
                      "service_outside_commit")
    inp = _validated(sv.input_model, input, f"The input to {name}", "service_input")
    _known_currencies(ctx, inp, f"The input to {name}")  # each Money's currency is a row of the books' currency table
    what = f"The result the service {name} returned"
    with approval_scope._service(ctx, caller, callee, sv.effect):
        out = _validated(sv.output_model, sv.fn(ctx, inp), what, "service_output")
    _known_currencies(ctx, out, what)
    try:
        result = out.model_dump(mode="json", by_alias=True)
    except Exception as e:  # noqa: BLE001  (a serializer that fails is refused, never passed on)
        raise Refusal(f"{what} could not be serialized ({type(e).__name__}).", "service_output") from None
    if (bad := _not_plain(result)) is not None:  # the model's own serializers ran in the dump
        raise Refusal(f"{what} is not valid: {bad}.", "service_output")
    return result


def install_preview(p: Plugin) -> dict:
    """What installing the plugin adds to a set of books, shown before it does."""
    return {"plugin": p.name, "version": p.version, "tables": sorted(p.manifest.owned_tables),
            "operations": sorted(p.manifest.contracts.items()), "metrics": sorted(p.manifest.metrics),
            "slots": sorted(p.manifest.slots)}


def _install_checks(conn: Connection, p: Plugin) -> None:
    from .common import Refusal
    from .migrate import applied_core_revision, core_at_or_after

    rows = _install_rows(conn)
    if p.name in rows:
        raise Refusal(f"The {p.name} plugin is already installed in these books.", "plugin_installed")
    for dep in p.manifest.depends:
        if rows.get(dep, {}).get("status") != "installed":
            raise Refusal(f"The {p.name} plugin needs {dep} installed and enabled in these books first.",
                          "plugin_dependency")
    if MESSAGE_SLOTS & set(p.slots):  # one message connector per set of books
        for other, row in rows.items():
            q = REGISTRY.plugins.get(other)
            if row["status"] == "installed" and q is not None and MESSAGE_SLOTS & set(q.slots):
                raise Refusal(f"These books already send messages through {other}; disable it first.",
                              "message_connector_installed")
    applied = applied_core_revision(conn)  # the books' core revision, not only the engine's
    if applied is None or not core_at_or_after(applied, p.manifest.after_core_revision):
        raise Refusal(f"The {p.name} plugin needs these books at core revision {p.manifest.after_core_revision} "
                      f"or later; they are at {applied or 'no core revision'}.", "plugin_core_revision")


def install_into(conn: Connection, p: Plugin, runtime_role: str | None = None) -> None:
    """Install on the caller's transaction, as the owner: the checks, then the
    plugin's branch to its head, the install record and, when a runtime role
    is named, its grants. A failure at any step leaves none of them."""
    from .grants import apply_grants
    from .migrate import upgrade_plugin

    _install_checks(conn, p)
    if p.migrations is not None:
        upgrade_plugin(conn, p.name)  # a reinstall after uninstall is a no-op upgrade
    _record(conn, p)
    if runtime_role:
        apply_grants(conn, runtime_role)


def install_record(conn: Connection, p: Plugin) -> None:
    """Record a plugin as installed in these books, after the install checks (its tables already exist)."""
    _install_checks(conn, p)
    _record(conn, p)


def _record(conn: Connection, p: Plugin) -> None:
    from . import models as m
    from .common import now

    conn.execute(m.plugin_install.insert().values(
        name=p.name, version=p.version, kind=p.kind, installed_at=now(), manifest_digest=p.manifest.digest(),
        artifact_digest=p.artifact_digest, after_core_revision=p.manifest.after_core_revision, status="installed"))


def install_plugin(engine: Engine, name: str, runtime_role: str | None = None) -> None:
    """As the owner, in one transaction: the plugin's branch, its record and, when a
    runtime role is named, its grants, verified before commit."""
    from .common import Refusal

    p = REGISTRY.plugins.get(name)
    if p is None or name == "core":
        raise Refusal(f"This engine has no plugin called {name}.", "no_plugin")
    check_installed(engine)  # the books' existing installs must match before anything changes
    with engine.begin() as conn:
        install_into(conn, p, runtime_role)


def _dependents(rows: dict[str, dict], name: str, statuses: tuple[str, ...]) -> list[str]:
    return sorted(n for n, r in rows.items() if r["status"] in statuses and n in REGISTRY.plugins
                  and name in REGISTRY.plugins[n].manifest.depends)


def disable_plugin(conn: Connection, name: str) -> None:
    """Its operations, metrics and slots stop for these books; its data stays readable."""
    from . import models as m
    from .common import Refusal

    check_books(conn)
    rows = _install_rows(conn)
    if rows.get(name, {}).get("status") != "installed":
        raise Refusal(f"The {name} plugin is not enabled in these books.", "plugin_not_enabled")
    dependents = _dependents(rows, name, ("installed",))
    if dependents:
        raise Refusal(f"The {name} plugin cannot be disabled while {', '.join(dependents)} depends on it.",
                      "plugin_has_dependents")
    conn.execute(m.plugin_install.update().where(m.plugin_install.c.name == name).values(status="disabled"))


def uninstall_plugin(conn: Connection, name: str) -> None:
    """Remove the install record; its tables and data are kept. Refused while
    any posting has one of its voucher types, or another plugin depends on it."""
    from . import models as m
    from .common import Refusal

    check_books(conn)
    rows = _install_rows(conn)
    if name not in rows:
        raise Refusal(f"The {name} plugin is not installed in these books.", "plugin_not_installed")
    dependents = _dependents(rows, name, ("installed", "disabled"))
    if dependents:
        raise Refusal(f"The {name} plugin cannot be uninstalled while {', '.join(dependents)} depends on it.",
                      "plugin_has_dependents")
    p = REGISTRY.plugins.get(name)
    types = list(p.manifest.voucher_types) if p else []
    kinds = types + [t + ":reversal" for t in types]
    if kinds and conn.execute(select(m.posting.c.id).where(m.posting.c.voucher_type.in_(kinds)).limit(1)).first():
        raise Refusal(f"The {name} plugin cannot be uninstalled: posted documents in these books depend on it. "
                      "It can be disabled instead.", "plugin_has_postings")
    conn.execute(m.plugin_install.delete().where(m.plugin_install.c.name == name))
