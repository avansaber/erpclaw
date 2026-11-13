"""The release rule's lint: every migration in a release is transactional and has no escape, so an
update's migrations can run in one transaction that commits only if all of them pass.

A migration runs on the one transaction's connection the runner gives it (`op`, `op.get_bind()`). It may not end that
transaction, step outside it, open another connection, or write anywhere but the books. The lint reads each migration
twice: its parsed Python (calls, names, imports and keywords, so a comment never counts) and the contents of its string
constants as SQL (comments, quoted literals and quoted identifiers blanked first, so `'committed'` or `-- COMMIT` never
count; a dollar-quoted body is kept, since a COMMIT in a DO block or procedure is an escape too). Docstrings and other
bare string statements are comments and are not read. A string that ends inside an open quote or comment is read
unblanked (fail closed). Matching is case-insensitive.

Discovery walks the source trees for every `migrations/versions` folder, so a new migration, or a new first-party
plugin's branch, is linted the day it is added.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE_TREES = ("packages", "plugins")  # core and the first-party plugins
_SKIP_DIRS = {"__pycache__", "node_modules", "build", "dist"}


@dataclass(frozen=True)
class PopulatedBooksRefusal:
    previous: str
    message: str
    reason: str


# Reviewed exceptions to successful upgrades of populated predecessors. This is
# the only registry: adding a boundary here requires a reviewed release-rule change.
# It exempts only the population guard, never any transaction/escape rule.
POPULATED_BOOKS_REFUSALS = {
    "0011": PopulatedBooksRefusal(
        previous="0010",
        message="older books are disposable and cannot be upgraded to ledger format 2",
        reason="Ledger format 2 was introduced on 4 Nov before any v3.0 release and already ships in preview 2; "
               "pre-format-2 populated books are deliberately refused, while empty books can upgrade.",
    ),
}

# rule -> why it is refused (the text a failure shows)
RULES = {
    "populated_books_refusal": "refusing populated books requires a reviewed revision and reason in "
                               "POPULATED_BOOKS_REFUSALS",
    "commit": "COMMIT ends the update's one transaction",
    "rollback": "ROLLBACK ends the update's one transaction",
    "autocommit_block": "an autocommit block runs outside the update's transaction",
    "concurrently": "CONCURRENTLY cannot run inside a transaction",
    "vacuum": "VACUUM cannot run inside a transaction",
    "alter_system": "ALTER SYSTEM cannot run inside a transaction and changes the server, not the books",
    "alter_type_add_value": "ALTER TYPE ... ADD VALUE is not usable until committed (refused unless proven "
                            "transactional on the supported majors)",
    "dblink": "dblink (or a foreign server) is a second connection outside the transaction",
    "second_connection": "a second engine or connection is outside the transaction the runner gives",
    "file_write": "a migration writes only the books, never a file",
    "network": "a migration never reaches the network",
    "process": "a migration never starts a process",
}

# --- discovery -------------------------------------------------------------------------------------------------


def versions_folders(repo: Path = REPO) -> list[Path]:
    """Every `migrations/versions` folder of core and the first-party plugins."""
    found = []
    for tree in SOURCE_TREES:
        for dirpath, dirnames, _ in os.walk(repo / tree):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            here = Path(dirpath)
            if here.name == "versions" and here.parent.name == "migrations":
                found.append(here)
    return sorted(found)


def migration_files(repo: Path = REPO) -> list[Path]:
    return sorted(p for folder in versions_folders(repo) for p in folder.glob("*.py"))


# --- SQL ---------------------------------------------------------------------------------------------------------

_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_I = re.IGNORECASE | re.DOTALL

SQL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("commit", re.compile(r"\bCOMMIT\b", _I)),  # `ON COMMIT` (a temporary table's clause) is skipped below
    ("commit", re.compile(r"\bPREPARE\s+TRANSACTION\b", _I)),
    ("rollback", re.compile(r"\bROLLBACK\b", _I)),
    ("autocommit_block", re.compile(r"\bAUTOCOMMIT\b", _I)),
    ("concurrently", re.compile(r"\bCONCURRENTLY\b", _I)),
    ("vacuum", re.compile(r"\bVACUUM\b", _I)),
    ("alter_system", re.compile(r"\bALTER\s+SYSTEM\b", _I)),
    ("alter_type_add_value", re.compile(r"\bALTER\s+TYPE\b[^;]*?\bADD\s+VALUE\b", _I)),
    ("dblink", re.compile(r"\bdblink\w*", _I)),
    ("dblink", re.compile(r"\bpostgres_fdw\b", _I)),
    ("file_write", re.compile(r"\bCOPY\b[^;]*?\b(TO|PROGRAM)\b", _I)),
    ("file_write", re.compile(r"\b(lo_export|pg_file_write|pg_write_file)\b", _I)),
]
# whole top-level statements (outside any dollar-quoted body): PostgreSQL's synonyms for COMMIT and ROLLBACK
STATEMENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    # COMMIT is already reported by SQL_PATTERNS; do not report it twice.
    ("commit", re.compile(r"^END(\s+(WORK|TRANSACTION))?(\s+AND\s+(NO\s+)?CHAIN)?$", _I)),
    ("rollback", re.compile(r"^ABORT(\s+(WORK|TRANSACTION))?(\s+AND\s+(NO\s+)?CHAIN)?$", _I)),
]
_ON_COMMIT = re.compile(r"\bON\s+$", re.IGNORECASE)
# An unfiltered read tests whether a table holds any rows. Filtered checks for
# invalid historical data are different, as are stored trigger/function bodies.
_POPULATION_READ = re.compile(
    r'\bSELECT\b[^;]*?\bFROM\s+(?:[\w.]+|"[^"]*")(?:\s*\.\s*"[^"]*")*'
    r'\s*(?=\)|;|$|LIMIT\b|THEN\b)', _I,
)
_DO_BLOCK = re.compile(r"\bDO\s+\$", _I)
_RAISE_EXCEPTION = re.compile(r"\bRAISE\s+(?:EXCEPTION|SQLSTATE)\b", _I)


def sanitize(sql: str) -> tuple[str, str, bool]:
    """(text, top, closed). `text` is `sql` with comments, quoted literals and quoted identifiers blanked to spaces
    (newlines kept, so offsets and lines still match) and dollar-quoted bodies kept, themselves sanitized; `top` blanks
    the dollar-quoted bodies too; `closed` is false when the string ends inside a quote, comment or body."""
    out, top = list(sql), list(sql)
    n, i, closed = len(sql), 0, True

    def blank(a: int, b: int, *targets: list) -> None:
        for k in range(a, b):
            if sql[k] != "\n":
                for t in targets:
                    t[k] = " "

    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            blank(i, j, out, top)
            i = j
        elif sql.startswith("/*", i):  # PostgreSQL's block comments nest
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            closed = closed and depth == 0
            blank(i, j, out, top)
            i = j
        elif c in "'\"":
            escapes = c == "'" and i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            j = i + 1
            while j < n:
                if escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:  # a doubled quote is the quote itself
                        j += 2
                        continue
                    break
                j += 1
            closed = closed and j < n
            blank(i + 1, min(j, n), out, top)
            i = j + 1
        elif c == "$" and (m := _DOLLAR.match(sql, i)) and not (i and (sql[i - 1].isalnum() or sql[i - 1] in "_$")):
            tag = m.group(0)
            end = sql.find(tag, m.end())
            if end < 0:
                closed, end = False, n
            inner, _, inner_closed = sanitize(sql[m.end():end])
            closed = closed and inner_closed
            out[m.end():end] = list(inner)
            blank(m.end(), end, top)
            i = end + len(tag)
        else:
            i += 1
    return "".join(out), "".join(top), closed


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail} ({RULES[self.rule]})"


def sql_findings(sql: str, path: str, line: int) -> list[Finding]:
    """The rules a string constant breaks, read as SQL; `line` is the string's first line in its file."""
    text, top, closed = sanitize(sql)
    if not closed:  # fail closed: an unterminated quote or comment could hide anything after it
        text = top = sql
    found = []
    # A DO block runs now; a CREATE FUNCTION merely installs a future guard.
    # Analyze each statement separately so a trigger and a later DO block cannot
    # mask one another (top blanks dollar bodies but preserves their offsets).
    offset = 0
    for statement in top.split(";"):
        body = text[offset:offset + len(statement)]
        if _DO_BLOCK.search(statement) and _POPULATION_READ.search(body) and (m := _RAISE_EXCEPTION.search(body)):
            found.append(Finding(path, line + text.count("\n", 0, offset + m.start()),
                                 "populated_books_refusal", "SQL population guard raises during upgrade"))
        offset += len(statement) + 1
    for rule, pattern in SQL_PATTERNS:
        for m in pattern.finditer(text):
            if rule == "commit" and m.group(0).upper() == "COMMIT" and _ON_COMMIT.search(text, 0, m.start()):
                continue  # CREATE TEMP TABLE ... ON COMMIT DROP: a clause, not the statement
            found.append(Finding(path, line + text.count("\n", 0, m.start()), rule, f"SQL {' '.join(m.group(0).split())!r}"))
    offset = 0
    for statement in top.split(";"):
        stripped = " ".join(statement.split())
        for rule, pattern in STATEMENT_PATTERNS:
            if pattern.match(stripped):
                lead = len(statement) - len(statement.lstrip())
                found.append(Finding(path, line + top.count("\n", 0, offset + lead), rule, f"SQL statement {stripped!r}"))
        offset += len(statement) + 1
    return found


# --- Python ------------------------------------------------------------------------------------------------------

NETWORK_MODULES = {"requests", "urllib", "urllib2", "urllib3", "httpx", "http", "socket", "smtplib", "ftplib",
                   "aiohttp", "telnetlib", "xmlrpc", "websocket", "websockets", "paramiko"}
DRIVER_MODULES = {"psycopg", "psycopg2", "asyncpg", "pg8000", "sqlite3", "pymysql", "aiosqlite"}
FILE_MODULES = {"shutil", "tempfile"}
PROCESS_MODULES = {"subprocess", "multiprocessing", "pty"}
ENGINE_NAMES = {"create_engine", "create_async_engine", "engine_from_config", "async_engine_from_config",
                "create_mock_engine", "create_pool_from_url", "raw_connection"}
CONNECT_NAMES = {"connect", "connect_async"}
FILE_WRITE_METHODS = {"write_text", "write_bytes", "touch", "mkdir", "makedirs", "unlink", "rmdir", "rmtree",
                      "symlink_to", "hardlink_to", "link_to", "chmod", "chown", "removedirs", "rename", "replace",
                      "write", "writelines", "truncate"}
OS_FILE_CALLS = {"remove", "unlink", "rename", "replace", "makedirs", "mkdir", "rmdir", "open", "fdopen", "write",
                 "truncate", "symlink", "link", "chmod", "chown", "mkfifo", "mknod", "utime"}
OS_PROCESS_PREFIXES = ("system", "popen", "spawn", "exec", "fork", "posix_spawn", "startfile")


def _root(module: str | None) -> str:
    return (module or "").split(".")[0]


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _module_rule(module: str) -> str | None:
    root = _root(module)
    if root in NETWORK_MODULES:
        return "network"
    if root in DRIVER_MODULES:
        return "second_connection"
    if root in FILE_MODULES:
        return "file_write"
    if root in PROCESS_MODULES:
        return "process"
    return None


def _writes(mode: ast.AST | None) -> bool:
    """True when an open() mode writes, or cannot be read from the source (fail closed)."""
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(set(mode.value) & set("wax+"))
    return True


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_string(node.left), _literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.found: list[Finding] = []
        self.consumed: set[int] = set()  # string constants already read (docstrings, f-string parts)
        self.aliases: dict[str, str] = {}

    def dotted(self, node: ast.AST) -> str:
        name = _dotted(node)
        root, dot, rest = name.partition(".")
        return self.aliases.get(root, root) + (dot + rest if dot else "")

    def add(self, node: ast.AST, rule: str, detail: str) -> None:
        self.found.append(Finding(self.path, getattr(node, "lineno", 0), rule, detail))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Python guards may read into a local variable before raising. Treat an
        # unfiltered table read plus a raise/assert in the same upgrade/helper as
        # a potential population refusal; boundary tests verify actual behavior.
        # Downgrades deliberately refuse and are outside the update path.
        if node.name != "downgrade":
            body = [n for statement in node.body
                    if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                            and isinstance(statement.value.value, str))
                    for n in ast.walk(statement)]
            reads_population = any(
                (sql := _literal_string(n)) is not None and id(n) not in self.consumed
                and _POPULATION_READ.search(sanitize(sql)[0])
                for n in body
            )
            if reads_population:
                for n in body:
                    if isinstance(n, (ast.Raise, ast.Assert)):
                        self.add(n, "populated_books_refusal", "Python population guard raises during upgrade")
        self.generic_visit(node)

    # comments: docstrings and bare string statements are never SQL
    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            self.consumed.add(id(node.value))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.aliases[alias.asname or _root(alias.name)] = alias.name if alias.asname else _root(alias.name)
            if rule := _module_rule(alias.name):
                self.add(node, rule, f"import {alias.name}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if rule := _module_rule(node.module or ""):
            self.add(node, rule, f"from {node.module} import ...")
        for alias in node.names:
            self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            if alias.name in ENGINE_NAMES:
                self.add(node, "second_connection", f"from {node.module} import {alias.name}")
            elif alias.name == "autocommit_block":
                self.add(node, "autocommit_block", f"from {node.module} import {alias.name}")
            elif node.module == "os" and alias.name.startswith(OS_PROCESS_PREFIXES):
                self.add(node, "process", f"from os import {alias.name}")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Follow ordinary aliases too: finish = bind.commit; finish().
        name = self.dotted(node.value)
        if name:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = name
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr in {"autocommit", "isolation_level"}:
                self.add(target, "autocommit_block", f"assignment to {self.dotted(target)}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in ENGINE_NAMES:
            self.add(node, "second_connection", node.id)
        elif node.id == "autocommit_block":
            self.add(node, "autocommit_block", node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in ENGINE_NAMES:
            self.add(node, "second_connection", _dotted(node) or node.attr)
        elif node.attr == "autocommit_block":
            self.add(node, "autocommit_block", _dotted(node) or node.attr)
        elif node.attr == "engine":  # the bind's engine opens connections of its own
            self.add(node, "second_connection", _dotted(node) or ".engine")
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg == "postgresql_concurrently" and not (isinstance(node.value, ast.Constant) and node.value.value is False):
            self.add(node.value, "concurrently", "postgresql_concurrently=")
        elif node.arg == "isolation_level":
            self.add(node.value, "autocommit_block", "isolation_level=")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self.dotted(node.func)
        base, _, short = name.rpartition(".")
        short = short or name
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if short in ENGINE_NAMES or short in CONNECT_NAMES:
            self.add(node, "second_connection", f"{name or short}()")
        elif short == "autocommit_block":
            self.add(node, "autocommit_block", f"{name or short}()")
        elif short == "commit":
            self.add(node, "commit", f"{name or short}()")
        elif short == "rollback":
            self.add(node, "rollback", f"{name or short}()")
        elif short == "open":
            if base == "os" or name == "os.fdopen":
                self.add(node, "file_write", f"{name}()")
            elif isinstance(node.func, ast.Name) or base in ("io", "codecs", "builtins"):  # open(file, mode)
                if _writes(kw.get("mode", node.args[1] if len(node.args) > 1 else None)):
                    self.add(node, "file_write", f"{name}() in write mode")
            elif _writes(kw.get("mode", node.args[0] if node.args else None)):  # Path.open(mode)
                self.add(node, "file_write", f"{name or short}() in write mode")
        elif (short in FILE_WRITE_METHODS
              and not (short == "replace" and isinstance(node.func, ast.Attribute)
                       and _literal_string(node.func.value) is not None)):
            self.add(node, "file_write", f"{name or short}()")
        elif base == "os" and short in OS_FILE_CALLS:
            self.add(node, "file_write", f"{name}()")
        elif base == "os" and short.startswith(OS_PROCESS_PREFIXES):
            self.add(node, "process", f"{name}()")
        elif short in ("__import__", "import_module") and node.args and isinstance(node.args[0], ast.Constant):
            if rule := _module_rule(str(node.args[0].value)):
                self.add(node, rule, f"{short}({node.args[0].value!r})")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        # Python's explicit string concatenation must not split a forbidden SQL keyword.
        sql = _literal_string(node)
        if sql is not None:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant):
                    self.consumed.add(id(sub))
            self.found += sql_findings(sql, self.path, node.lineno)
        else:
            self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        """An f-string reads as SQL with each replacement field standing in as `_`."""
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("_")
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant):
                self.consumed.add(id(sub))
        self.found += sql_findings("".join(parts), self.path, node.lineno)
        for value in node.values:  # the replacement fields' own expressions are Python
            if isinstance(value, ast.FormattedValue):
                self.visit(value.value)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self.consumed:
            self.found += sql_findings(node.value, self.path, node.lineno)


def lint_source(source: str, path: str = "<migration>") -> list[Finding]:
    """Every rule the migration's source breaks, in line order."""
    tree = ast.parse(source, filename=path)
    visitor = _Visitor(path)
    for node in ast.walk(tree):  # docstrings first, so the visit below skips them wherever they are
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                visitor.consumed.add(id(first.value))
    visitor.visit(tree)
    revision = next((n.value.value for n in tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "revision" for t in n.targets)
                     and isinstance(n.value, ast.Constant)), None)
    findings = [f for f in visitor.found
                if f.rule != "populated_books_refusal" or revision not in POPULATED_BOOKS_REFUSALS]
    return sorted(set(findings), key=lambda f: (f.line, f.rule, f.detail))


def lint_file(path: Path, repo: Path = REPO) -> list[Finding]:
    try:
        shown = str(path.relative_to(repo))
    except ValueError:
        shown = str(path)
    return lint_source(path.read_text(encoding="utf-8"), shown)
