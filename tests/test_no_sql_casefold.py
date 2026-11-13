"""SQL never folds case on owner text. The engine's code calls no SQL lower() or upper(), no ILIKE (nor SQLAlchemy's
ilike, notilike, icontains, istartswith and iendswith, which compile to ILIKE or to lower()), no case-blind regular
expression match and no COLLATE: a comparison without regard to case goes through the stored key
(erpclaw_core.textkey), compared by equality, because the database's case rules are not Python's.
Engine-issued document numbers instead normalise the owner's input in Python to their fixed issued form,
then compare the stored column by plain equality.

Every text column of the books can hold the owner's words, so the calls themselves are refused, on any column. It
reads source: SQL assembled at run time from pieces is not found here, and code review covers that."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# the engine's code: every package's and plugin's source, its migrations included; tests may plant what this refuses
SOURCES = sorted(p for base in ("packages", "plugins") for p in (ROOT / base).glob("*/src/**/*.py"))

SQL_FUNCTIONS = {"lower", "upper"}  # reached as func.lower, sa.func.upper, sqlalchemy.func.LOWER
CASE_BLIND = {"ilike", "notilike", "not_ilike", "icontains", "istartswith", "iendswith", "collate"}
# the same in SQL text: lower( or upper(, ILIKE, COLLATE, and the case-blind regular expression operators ~* and !~*
RAW = re.compile(r"\b(?:lower|upper)\s*\(|\bilike\b|\bcollate\b|~\*", re.IGNORECASE)


def _docstrings(tree: ast.AST) -> set[int]:
    """Docstrings are prose: they may name what this refuses."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value,
                                                                                                    str):
                out.add(id(first.value))
    return out


def _is_func(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "func") or (isinstance(node, ast.Attribute) and node.attr == "func")


def findings(source: str, path: str = "<planted>") -> list[str]:
    """Each place in `source` where SQL would fold case, as `path:line: code`."""
    tree = ast.parse(source)
    prose = _docstrings(tree)
    # sqlalchemy's own collate(), under whatever name it was imported
    collate = {a.asname or a.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and (node.module or "").split(".")[0] == "sqlalchemy" for a in node.names if a.name == "collate"}
    lines = source.splitlines()
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            hit = (node.attr.lower() in SQL_FUNCTIONS and _is_func(node.value)) or node.attr in CASE_BLIND
        elif isinstance(node, ast.Name):
            hit = node.id in collate
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = id(node) not in prose and bool(RAW.search(node.value))
        else:
            continue
        if hit:
            hits.add((node.lineno, lines[node.lineno - 1].strip()))
    return [f"{path}:{line}: {code}" for line, code in sorted(hits)]


def test_the_engine_never_folds_case_in_sql():
    assert len(SOURCES) > 50  # the glob found the engine, not an empty folder
    found = [f for p in SOURCES for f in findings(p.read_text(encoding="utf-8"), str(p.relative_to(ROOT)))]
    assert found == [], "compare owner text by its stored key (erpclaw_core.textkey), not in SQL:\n" + "\n".join(found)


PLANTED = [
    # the two queries that folded case in SQL, as they were written
    "from sqlalchemy import func\nq = q.where(func.lower(m.party.c.city) == city.strip().lower())",
    "q = q.where(p.c.city.ilike(city))",
    "import sqlalchemy as sa\nq = q.where(sa.func.upper(m.party.c.name) == said.upper())",
    "import sqlalchemy\nq = q.where(sqlalchemy.func.LOWER(m.party.c.name) == said)",
    "q = q.where(m.party.c.name.notilike(said))",
    "q = q.where(m.party.c.name.icontains(said))",
    "q = q.where(m.party.c.name.istartswith(said))",
    "q = q.where(m.party.c.name.iendswith(said))",
    "q = q.where(m.party.c.city.collate('C') == said)",
    "from sqlalchemy import collate\nq = q.where(collate(m.party.c.city, 'und-x-icu') == said)",
    "from sqlalchemy import collate as c\nq = q.where(c(m.party.c.city, 'C') == said)",
    "q = q.where(m.party.c.city.op('~*')(said))",
    "conn.execute(text('SELECT id FROM party WHERE city ILIKE :c'), {'c': said})",
    "conn.execute(text('SELECT id FROM party WHERE lower(city) = :c'), {'c': said.lower()})",
    "conn.exec_driver_sql(f\"SELECT id FROM party WHERE UPPER (name) = '{said}'\")",
    "conn.execute(text('SELECT id FROM party ORDER BY name COLLATE \"C\"'))",
    "conn.execute(text('SELECT id FROM party WHERE city !~* :c'), {'c': said})",
]


@pytest.mark.parametrize("source", PLANTED)
def test_each_way_of_folding_case_in_sql_is_found(source):
    found = findings(source)
    assert len(found) == 1, found
    assert found[0].startswith(f"<planted>:{len(source.splitlines())}: ")


def test_case_folding_in_python_and_prose_are_not_found():
    source = '''"""Folds case in Python, never with SQL lower(), upper(), ILIKE or COLLATE."""
from erpclaw_core.textkey import text_key


def match(rows, said):
    """Unlike city ILIKE :c, this keeps every letter."""
    # never func.lower(party.city) or party.city.ilike(said)
    key = text_key(said)
    return [r for r in rows if r.city_key == key or r.name.lower() == said.lower() or r.code.upper() == said.upper()]


NOTE = "collateral and the lower limit are words; lowered (case) is too"
'''
    assert findings(source) == []
