"""Static checks on our own plugin code (plugin interface spec, section 5):
a plugin package writes only the tables it owns, and never posting or
ledger_entry (the posting service is their only writer); and it imports no
other plugin's package (another plugin is reached only through core's
call_service).

It inspects source, so SQL assembled at run time, reflective calls and writes
hidden in helpers are not found here; code review covers those."""
from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from pathlib import Path

from sqlalchemy import Table

WRITES = {"insert", "update", "delete"}
RAW_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE)\b", re.I)


def table_names(module_names: list[str]) -> dict[str, Table]:
    """Python names bound to Tables in the given modules (and their submodules)."""
    found: dict[str, Table] = {}
    for name in module_names:
        mod = importlib.import_module(name)
        mods = [mod]
        if hasattr(mod, "__path__"):
            mods += [importlib.import_module(f"{name}.{i.name}") for i in pkgutil.iter_modules(mod.__path__)]
        for m in mods:
            found.update({k: v for k, v in vars(m).items() if isinstance(v, Table)})
    return found


def _target(node: ast.AST, tables: dict[str, Table]) -> Table | None:
    name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
    return tables.get(name) if name else None


def violations(package_dir: Path, owner: str, tables: dict[str, Table]) -> list[str]:
    out = []
    for f in sorted(package_dir.rglob("*.py")):
        if "migrations" in f.relative_to(package_dir).parts:
            continue  # its branch: DDL run once as the owner at install; the loader checks it creates exactly its tables
        tree = ast.parse(f.read_text(), str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn, table = node.func, None
                if isinstance(fn, ast.Attribute) and fn.attr in WRITES:  # m.party.insert()
                    table = _target(fn.value, tables)
                elif isinstance(fn, ast.Name) and fn.id in WRITES and node.args:  # insert(m.party)
                    table = _target(node.args[0], tables)
                if table is not None and table.info.get("owner") != owner:
                    out.append(f"{f.name}:{node.lineno} writes {table.name} (owned by {table.info.get('owner')})")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and RAW_WRITE.search(node.value):
                out.append(f"{f.name}:{node.lineno} raw SQL write")
    return out


def other_plugin_imports(package_dir: Path, own: str) -> list[str]:
    """Imports, in a plugin package's code, of any engine package but core and its own (absolute imports of an
    `erpclaw_` package; a relative import stays inside its own package)."""
    out = []
    for f in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(f.read_text(), str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                if top.startswith("erpclaw_") and top not in ("erpclaw_core", own):
                    out.append(f"{f.name}:{node.lineno} imports {mod}")
    return out
