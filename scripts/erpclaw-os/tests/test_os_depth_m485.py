"""M485 depth: behavioural evidence for 7 erpclaw-os actions.

Prior state (read before writing anything below):
- `validate-module` had unit tests calling validate_module_static() and
  asserting pass/fail shapes in test_validate_module.py and
  test_constitution_articles.py, plus routability against real modules in
  test_existing_modules.py. None read the database back.
- `list-articles`, `build-table-registry` had helper-level shape tests
  (registry size, core ownership) but never pinned exact article payloads
  or exact registry maps against a controlled source tree, nor proved the
  database was untouched.
- `schema-plan`, `schema-apply`, `schema-rollback`, `schema-drift` had
  handler tests in test_schema_migrator.py asserting result strings such as
  "planned" or "applied", with one lifecycle test using catalog reads. None
  read the stored migration row back with exact values through the seam, and
  none pinned money TEXT or ledger silence.

Every test below drives the REAL handler against a fresh core database,
reads the stored rows back with PyPika-built queries through
erpclaw_lib.query on a connection from erpclaw_lib.db.get_connection, asks
catalog questions only through erpclaw_lib.seam, and compares exact values.
Money is TEXT: exact Decimal strings, never float, never approximate.

Ledger scope, stated once so no later reader adds a balance assertion that
cannot hold: none of these 7 handlers reaches the general ledger. Plan
writes one erpclaw_schema_migration row, apply creates tables and flips that
row to applied, rollback drops tables and flips it to rolled_back, drift /
list / registry / validate are reads. Every success test pins gl_entry,
stock_ledger_entry and payment_ledger_entry counts unchanged.

Per-action depth (stored row unless noted):
- build-table-registry: registry effect (exact table to owner map on a
  controlled source tree; read-only, snapshot proves no write).
- list-articles: stored payload (read-only; payload mirrors the constitution
  rows exactly, snapshot proves no write).
- schema-plan: stored row (erpclaw_schema_migration planned row read back).
- schema-apply: stored row (tables exist via seam plus migration row flipped
  to applied; money TEXT pinned on the new columns and one exact row).
- schema-rollback: stored row (tables gone via seam, backup tables hold exact
  money strings, migration row flipped to rolled_back).
- schema-drift: stored finding (read-only; exact finding rows mirror the
  file versus database gap, snapshot proves no write).
- validate-module: stored verdict (payload mirrors the module files exactly:
  pass rows for a valid module, exact violation rows for a float-money
  module; snapshot proves no write).
"""
import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import textwrap
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_OS_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_OS_DIR)
_SETUP_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup")
_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib")):
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, _IN_TREE_LIB)
if _OS_DIR not in sys.path:
    sys.path.insert(0, _OS_DIR)

from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, fn, insert_row  # noqa: E402
from erpclaw_lib import seam  # noqa: E402


def _load_os(name):
    spec = importlib.util.spec_from_file_location(
        "os_m485_" + name, os.path.join(_OS_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VM = _load_os("validate_module")
CONST = _load_os("constitution")
SM = _load_os("schema_migrator")
SD = _load_os("schema_diff")
DBQ = _load_os("db_query")

_INIT_SCHEMA_PATH = os.path.join(_SETUP_DIR, "init_schema.py")


def _init_full_db(db_path):
    spec = importlib.util.spec_from_file_location(
        "init_schema_m485", _INIT_SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db(db_path)


def ns(**kwargs):
    return argparse.Namespace(**kwargs)


def _call_ok_handler(fn, args):
    buf = io.StringIO()

    def _fake_exit(code=0):
        raise SystemExit(code)

    try:
        with patch("sys.stdout", buf), patch("sys.exit", side_effect=_fake_exit):
            fn(args)
    except SystemExit:
        pass
    out = buf.getvalue().strip()
    if not out:
        return {"status": "error", "message": "no output captured"}
    return json.loads(out)


def _u():
    return str(uuid.uuid4())


SNAPSHOT_TABLES = (
    "erpclaw_schema_migration",
    "gl_entry",
    "stock_ledger_entry",
    "payment_ledger_entry",
    "company",
    "audit_log",
)


def _snapshot(conn, db_path, tables=None):
    names = tables or SNAPSHOT_TABLES
    snap = {}
    for name in names:
        if not seam.table_exists(name, db_path):
            snap[name] = "<missing>"
            continue
        t = Table(name)
        rows = conn.execute(Q.from_(t).select(t.star).get_sql()).fetchall()
        snap[name] = sorted(repr(dict(r)) for r in rows)
    return snap


def _all(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _row(conn, table, row_id):
    t = Table(table)
    q = Q.from_(t).select(t.star).where(t.id == P())
    found = conn.execute(q.get_sql(), (row_id,)).fetchone()
    assert found is not None, "%s %s not found" % (table, row_id)
    return dict(found)


def _count(conn, table, db_path):
    if not seam.table_exists(table, db_path):
        return 0
    t = Table(table)
    q = Q.from_(t).select(fn.Count("*").as_("n"))
    return conn.execute(q.get_sql()).fetchone()["n"]


def _insert(conn, table, row):
    sql, _cols = insert_row(table, {key: P() for key in row})
    conn.execute(sql, tuple(row.values()))
    conn.commit()


def _migration_row(conn, migration_id):
    return _row(conn, "erpclaw_schema_migration", migration_id)


@pytest.fixture(scope="module")
def template_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("m485tpl") / "template.sqlite")
    _init_full_db(path)
    SM.ensure_migration_table(path)
    return path


@pytest.fixture
def db_path(template_path, tmp_path):
    import shutil
    path = str(tmp_path / "m485.sqlite")
    shutil.copyfile(template_path, path)
    prev = os.environ.get("ERPCLAW_DB_PATH")
    os.environ["ERPCLAW_DB_PATH"] = path
    yield path
    if prev is None:
        os.environ.pop("ERPCLAW_DB_PATH", None)
    else:
        os.environ["ERPCLAW_DB_PATH"] = prev


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


def _write_valid_module(base, name="m485claw"):
    module = os.path.join(base, name)
    os.makedirs(os.path.join(module, "scripts", "tests"), exist_ok=True)
    with open(os.path.join(module, "init_db.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS m485claw_item (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    price       TEXT NOT NULL DEFAULT '0',
                    company_id  TEXT NOT NULL,
                    created_at  TEXT DEFAULT (datetime('now'))
                );
            \"\"\"
        """))
    with open(os.path.join(module, "scripts", "db_query.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            import os, sys
            sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
            from erpclaw_lib.response import ok, err
            def main():
                ok({"message": "ok"})
            if __name__ == "__main__":
                main()
        """))
    with open(os.path.join(module, "SKILL.md"), "w") as handle:
        handle.write(textwrap.dedent("""\
            ---
            name: m485claw
            version: 1.0.0
            description: Depth fixture module
            author: test
            scripts:
              - scripts/db_query.py
            ---

            # m485claw

            ## Actions

            | Action | Description |
            |--------|-------------|
            | `m485-add-item` | Add an item |
            | `status` | Check status |
        """))
    with open(os.path.join(module, "scripts", "tests", "__init__.py"), "w") as handle:
        handle.write("")
    with open(os.path.join(module, "scripts", "tests", "test_basic.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            def test_m485_add_item():
                assert True

            def test_status():
                assert True
        """))
    return module


def _write_float_money_module(base, name="m485violclaw"):
    module = os.path.join(base, name)
    os.makedirs(os.path.join(module, "scripts", "tests"), exist_ok=True)
    with open(os.path.join(module, "init_db.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS m485violclaw_item (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    price       REAL NOT NULL,
                    company_id  TEXT NOT NULL
                );
            \"\"\"
        """))
    with open(os.path.join(module, "scripts", "db_query.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            import os, sys
            sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
            from erpclaw_lib.response import ok, err
            def main():
                ok({"message": "ok"})
            if __name__ == "__main__":
                main()
        """))
    with open(os.path.join(module, "SKILL.md"), "w") as handle:
        handle.write(textwrap.dedent("""\
            ---
            name: m485violclaw
            version: 1.0.0
            description: Depth violation fixture
            author: test
            scripts:
              - scripts/db_query.py
            ---

            # m485violclaw

            ## Actions

            | Action | Description |
            |--------|-------------|
            | `m485viol-add-item` | Add an item |
            | `status` | Check status |
        """))
    with open(os.path.join(module, "scripts", "tests", "__init__.py"), "w") as handle:
        handle.write("")
    with open(os.path.join(module, "scripts", "tests", "test_basic.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            def test_m485viol_add_item():
                assert True

            def test_status():
                assert True
        """))
    return module


def _write_schema_module(base, name="m485schem claw".replace(" ", "")):
    module = os.path.join(base, name)
    os.makedirs(module, exist_ok=True)
    with open(os.path.join(module, "init_db.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS m485_widget (
                    id          TEXT PRIMARY KEY,
                    company_id  TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    price       TEXT NOT NULL DEFAULT '0.00',
                    status      TEXT DEFAULT 'active',
                    created_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_m485_widget_company
                    ON m485_widget(company_id);

                CREATE TABLE IF NOT EXISTS m485_order (
                    id          TEXT PRIMARY KEY,
                    company_id  TEXT NOT NULL,
                    widget_id   TEXT,
                    quantity    INTEGER NOT NULL DEFAULT 1,
                    total       TEXT NOT NULL DEFAULT '0.00',
                    status      TEXT DEFAULT 'draft',
                    created_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_m485_order_company
                    ON m485_order(company_id);
            \"\"\"
        """))
    with open(os.path.join(module, "SKILL.md"), "w") as handle:
        handle.write(textwrap.dedent("""\
            ---
            name: m485schemaclaw
            version: 1.0.0
            description: Schema depth fixture
            author: test
            scripts:
              - scripts/db_query.py
            ---

            # m485schemaclaw
        """))
    return module


def _write_registry_src(base):
    src = os.path.join(base, "srcroot")
    mod_a = os.path.join(src, "m485aclaw")
    mod_b = os.path.join(src, "m485bclaw")
    os.makedirs(mod_a, exist_ok=True)
    os.makedirs(mod_b, exist_ok=True)
    with open(os.path.join(mod_a, "init_db.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS m485aclaw_x (
                    id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS m485aclaw_y (
                    id TEXT PRIMARY KEY
                );
            \"\"\"
        """))
    with open(os.path.join(mod_b, "init_db.py"), "w") as handle:
        handle.write(textwrap.dedent("""\
            DDL = \"\"\"
                CREATE TABLE IF NOT EXISTS m485bclaw_a (
                    id TEXT PRIMARY KEY
                );
            \"\"\"
        """))
    return src


# ---------------------------------------------------------------------------
# build-table-registry: registry effect (read-only).
# This action never reaches the ledger; the snapshot is the no-write proof
# and no ledger assertion can hold for it.
# ---------------------------------------------------------------------------
class TestBuildTableRegistryDepth:
    def test_registry_maps_exact_tables_and_writes_nothing(
            self, conn, db_path, tmp_path):
        src = _write_registry_src(str(tmp_path))
        before = _snapshot(conn, db_path)
        ledgers_before = {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry", "payment_ledger_entry")}
        result = _call_ok_handler(
            DBQ.handle_build_table_registry, ns(src_root=src))
        assert result.get("status") == "ok", result
        assert result["registry"] == {
            "m485aclaw_x": "m485aclaw",
            "m485aclaw_y": "m485aclaw",
            "m485bclaw_a": "m485bclaw",
        }
        assert result["total_tables"] == 3
        assert result["total_modules"] == 2
        assert result["by_module"] == {
            "m485aclaw": ["m485aclaw_x", "m485aclaw_y"],
            "m485bclaw": ["m485bclaw_a"],
        }
        direct = VM.build_table_ownership_registry(src)
        assert direct == result["registry"]
        assert _snapshot(conn, db_path) == before
        assert {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry",
            "payment_ledger_entry")} == ledgers_before
        assert seam.table_exists("m485aclaw_x", db_path) is False
        assert seam.table_exists("company", db_path) is True

    def test_refuses_bad_src_root_and_writes_nothing(
            self, conn, db_path, tmp_path):
        before = _snapshot(conn, db_path)
        bad = os.path.join(str(tmp_path), "no-such-src")
        result = _call_ok_handler(
            DBQ.handle_build_table_registry, ns(src_root=bad))
        assert result.get("status") == "error", result
        assert "does not exist" in result.get("message", "")
        assert bad in result.get("message", "")
        assert _snapshot(conn, db_path) == before


# ---------------------------------------------------------------------------
# list-articles: stored payload (read-only).
# This action never reaches the ledger; the snapshot is the no-write proof
# and no ledger assertion can hold for it.
# ---------------------------------------------------------------------------
class TestListArticlesDepth:
    def test_all_lists_21_with_exact_names_and_writes_nothing(
            self, conn, db_path):
        before = _snapshot(conn, db_path)
        result = _call_ok_handler(
            DBQ.handle_list_articles, ns(article_type="all"))
        assert result.get("status") == "ok", result
        assert result["count"] == 21
        assert len(result["articles"]) == 21
        by_number = {a["number"]: a for a in result["articles"]}
        assert by_number[1]["name"] == "Table Prefix Enforcement"
        assert by_number[2]["name"] == "Money is TEXT"
        assert by_number[3]["name"] == "UUID Primary Keys"
        assert by_number[9]["enforcement"] == "runtime"
        assert by_number[1]["enforcement"] == "static"
        assert result["articles"] == CONST.ARTICLES
        static = _call_ok_handler(
            DBQ.handle_list_articles, ns(article_type="static"))
        assert static["count"] == 14
        assert all(a["enforcement"] == "static" for a in static["articles"])
        runtime = _call_ok_handler(
            DBQ.handle_list_articles, ns(article_type="runtime"))
        assert runtime["count"] == 7
        assert all(a["enforcement"] == "runtime" for a in runtime["articles"])
        assert _snapshot(conn, db_path) == before

    def test_refuses_invalid_article_type_via_cli_and_writes_nothing(
            self, conn, db_path):
        before = _snapshot(conn, db_path)
        script = os.path.join(_OS_DIR, "db_query.py")
        proc = subprocess.run(
            [sys.executable, script, "--action", "list-articles",
             "--article-type", "bogus"],
            capture_output=True, text=True, timeout=30)
        assert proc.returncode != 0
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert "invalid choice" in combined.lower()
        assert _snapshot(conn, db_path) == before


# ---------------------------------------------------------------------------
# validate-module: stored verdict (read-only).
# This action never reaches the ledger; the snapshot is the no-write proof
# and no ledger assertion can hold for it.
# ---------------------------------------------------------------------------
class TestValidateModuleDepth:
    def test_valid_module_passes_with_exact_articles_and_writes_nothing(
            self, conn, db_path, tmp_path):
        module = _write_valid_module(str(tmp_path))
        before = _snapshot(conn, db_path)
        result = VM.validate_module_static(module)
        assert result["result"] == "pass"
        assert result["module_name"] == "m485claw"
        assert result["violations"] == []
        assert result["articles"][1] == "pass"
        assert result["articles"][2] == "pass"
        assert result["articles"][3] == "pass"
        handled = _call_ok_handler(
            DBQ.handle_validate_module, ns(
                module_path=module, validation_type="static",
                db_path=None))
        assert handled.get("status") == "ok", handled
        assert handled["result"] == "pass"
        assert handled["module_name"] == "m485claw"
        assert handled["violations"] == []
        assert _snapshot(conn, db_path) == before

    def test_float_money_fails_with_exact_violation_and_writes_nothing(
            self, conn, db_path, tmp_path):
        module = _write_float_money_module(str(tmp_path))
        before = _snapshot(conn, db_path)
        result = VM.validate_module_static(module)
        assert result["result"] == "fail"
        assert result["articles"][2] == "fail"
        art2 = [v for v in result["violations"] if v.get("article") == 2]
        assert len(art2) == 1
        assert art2[0]["table"] == "m485violclaw_item"
        assert art2[0]["column"] == "price"
        assert art2[0]["type"] == "REAL"
        assert "should be TEXT" in art2[0]["message"]
        assert _snapshot(conn, db_path) == before

    def test_refuses_bad_module_path_and_writes_nothing(
            self, conn, db_path, tmp_path):
        before = _snapshot(conn, db_path)
        bad = os.path.join(str(tmp_path), "no-such-module")
        result = _call_ok_handler(
            DBQ.handle_validate_module, ns(
                module_path=bad, validation_type="static",
                db_path=None))
        assert result.get("status") == "error", result
        assert "does not exist" in result.get("message", "")
        assert bad in result.get("message", "")
        assert _snapshot(conn, db_path) == before


# ---------------------------------------------------------------------------
# schema-plan: stored row (erpclaw_schema_migration).
# This action never reaches the ledger; ledger counts are pinned unchanged
# and no leg assertion can hold for it.
# ---------------------------------------------------------------------------
class TestSchemaPlanDepth:
    def test_plan_inserts_exact_migration_row_and_creates_nothing(
            self, conn, db_path, tmp_path):
        module = _write_schema_module(str(tmp_path))
        before_ledgers = {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry", "payment_ledger_entry")}
        before_migrations = _count(conn, "erpclaw_schema_migration", db_path)
        result = SM.handle_schema_plan(ns(
            module_path=module, db_path=db_path, src_root=None))
        assert result["result"] == "planned", result
        assert result["module_name"] == os.path.basename(module)
        assert result["new_tables"] == ["m485_order", "m485_widget"]
        assert result["new_columns"] == []
        assert result["ddl_count"] == 4
        assert len(result["ddl_statements"]) == 4
        assert any("m485_widget" in s for s in result["ddl_statements"])
        assert any("m485_order" in s for s in result["ddl_statements"])
        assert any("price" in s and "TEXT" in s for s in result["ddl_statements"])
        row = _migration_row(conn, result["migration_id"])
        assert row["module_name"] == os.path.basename(module)
        assert row["migration_type"] == "create"
        assert row["status"] == "planned"
        assert row["planned_at"] is not None
        stored_ddl = json.loads(row["ddl_statements"])
        assert len(stored_ddl) == 4
        assert _count(conn, "erpclaw_schema_migration", db_path) == before_migrations + 1
        assert seam.table_exists("m485_widget", db_path) is False
        assert seam.table_exists("m485_order", db_path) is False
        assert {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry",
            "payment_ledger_entry")} == before_ledgers

    def test_refuses_missing_path_and_writes_nothing(
            self, conn, db_path, tmp_path):
        before = _snapshot(conn, db_path)
        result = SM.handle_schema_plan(ns(
            module_path=None, db_path=db_path, src_root=None))
        assert "error" in result
        assert "--module-path is required" in result["error"]
        assert _snapshot(conn, db_path) == before
        module = _write_schema_module(str(tmp_path))
        missing = os.path.join(module, "no-such-dir")
        result = SM.handle_schema_plan(ns(
            module_path=missing, db_path=db_path, src_root=None))
        assert result["result"] == "error"
        assert "No init_db.py found" in result["error"]
        assert _snapshot(conn, db_path) == before


# ---------------------------------------------------------------------------
# schema-apply: stored row (tables plus migration row flipped to applied).
# Money is TEXT on the new columns; one exact money row is read back with
# Decimal. This action never reaches the ledger.
# ---------------------------------------------------------------------------
class TestSchemaApplyDepth:
    def test_apply_creates_tables_and_marks_row_applied(
            self, conn, db_path, tmp_path):
        module = _write_schema_module(str(tmp_path))
        plan = SM.handle_schema_plan(ns(
            module_path=module, db_path=db_path, src_root=None))
        assert plan["result"] == "planned", plan
        ledgers_before = {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry", "payment_ledger_entry")}
        result = SM.handle_schema_apply(ns(
            migration_id=plan["migration_id"], db_path=db_path))
        assert result["result"] == "applied", result
        assert result["migration_id"] == plan["migration_id"]
        assert result["tables_created"] == ["m485_order", "m485_widget"]
        assert result["ddl_executed"] == 4
        assert seam.table_exists("m485_widget", db_path) is True
        assert seam.table_exists("m485_order", db_path) is True
        assert "price" in seam.column_names("m485_widget", db_path)
        assert "total" in seam.column_names("m485_order", db_path)
        described = seam.describe_table("m485_widget", db_path)
        price_col = [c for c in described["columns"] if c["name"] == "price"][0]
        assert "TEXT" in price_col["type"]
        row = _migration_row(conn, plan["migration_id"])
        assert row["status"] == "applied"
        assert row["applied_at"] is not None
        wid = _u()
        _insert(conn, "m485_widget", {
            "id": wid, "company_id": _u(), "name": "Gizmo",
            "price": "19.99", "status": "active"})
        stored = _row(conn, "m485_widget", wid)
        assert stored["price"] == "19.99"
        assert Decimal(stored["price"]) == Decimal("19.99")
        assert stored["name"] == "Gizmo"
        assert {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry",
            "payment_ledger_entry")} == ledgers_before

    def test_refuses_missing_id_and_writes_nothing(
            self, conn, db_path, tmp_path):
        module = _write_schema_module(str(tmp_path))
        before = _snapshot(conn, db_path)
        result = SM.handle_schema_apply(ns(
            migration_id=None, db_path=db_path))
        assert "error" in result
        assert "--migration-id is required" in result["error"]
        assert _snapshot(conn, db_path) == before
        result = SM.handle_schema_apply(ns(
            migration_id="no-such-migration", db_path=db_path))
        assert result["result"] == "error"
        assert "not found" in result["error"]
        assert _snapshot(conn, db_path) == before
        assert seam.table_exists("m485_widget", db_path) is False


# ---------------------------------------------------------------------------
# schema-rollback: stored row (tables gone, backups hold exact rows).
# Money strings in the backup rows are compared as exact Decimal strings.
# This action never reaches the ledger.
# ---------------------------------------------------------------------------
class TestSchemaRollbackDepth:
    def test_rollback_drops_and_backs_up_exact_rows(
            self, conn, db_path, tmp_path):
        module = _write_schema_module(str(tmp_path))
        plan = SM.handle_schema_plan(ns(
            module_path=module, db_path=db_path, src_root=None))
        applied = SM.handle_schema_apply(ns(
            migration_id=plan["migration_id"], db_path=db_path))
        assert applied["result"] == "applied", applied
        wid = _u()
        oid = _u()
        _insert(conn, "m485_widget", {
            "id": wid, "company_id": _u(), "name": "Gizmo",
            "price": "9.99", "status": "active"})
        _insert(conn, "m485_order", {
            "id": oid, "company_id": _u(), "widget_id": wid,
            "quantity": 2, "total": "19.98", "status": "draft"})
        ledgers_before = {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry", "payment_ledger_entry")}
        result = SM.handle_schema_rollback(ns(
            migration_id=plan["migration_id"], db_path=db_path))
        assert result["result"] == "rolled_back", result
        assert result["tables_dropped"] == ["m485_order", "m485_widget"]
        assert len(result["backups_created"]) == 2
        by_original = {b["original"]: b for b in result["backups_created"]}
        assert by_original["m485_widget"]["rows"] == 1
        assert by_original["m485_order"]["rows"] == 1
        assert by_original["m485_widget"]["backup"] == (
            "m485_widget_backup_" + plan["migration_id"][:8])
        assert seam.table_exists("m485_widget", db_path) is False
        assert seam.table_exists("m485_order", db_path) is False
        assert seam.table_exists(
            by_original["m485_widget"]["backup"], db_path) is True
        backup_widget = _row(
            conn, by_original["m485_widget"]["backup"], wid)
        assert backup_widget["price"] == "9.99"
        assert Decimal(backup_widget["price"]) == Decimal("9.99")
        backup_order = _row(
            conn, by_original["m485_order"]["backup"], oid)
        assert backup_order["total"] == "19.98"
        assert Decimal(backup_order["total"]) == Decimal("19.98")
        row = _migration_row(conn, plan["migration_id"])
        assert row["status"] == "rolled_back"
        assert row["rolled_back_at"] is not None
        assert {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry",
            "payment_ledger_entry")} == ledgers_before

    def test_refuses_planned_not_applied_and_writes_nothing(
            self, conn, db_path, tmp_path):
        module = _write_schema_module(str(tmp_path))
        plan = SM.handle_schema_plan(ns(
            module_path=module, db_path=db_path, src_root=None))
        before = _snapshot(conn, db_path)
        result = SM.handle_schema_rollback(ns(
            migration_id=None, db_path=db_path))
        assert "error" in result
        assert "--migration-id is required" in result["error"]
        assert _snapshot(conn, db_path) == before
        result = SM.handle_schema_rollback(ns(
            migration_id=plan["migration_id"], db_path=db_path))
        assert result["result"] == "error"
        assert "expected 'applied'" in result["error"]
        assert _snapshot(conn, db_path) == before
        assert seam.table_exists("m485_widget", db_path) is False


# ---------------------------------------------------------------------------
# schema-drift: stored finding (read-only).
# This action never reaches the ledger; the snapshot is the no-write proof
# and no ledger assertion can hold for it.
# ---------------------------------------------------------------------------
class TestSchemaDriftDepth:
    def test_missing_column_detected_with_exact_finding_and_writes_nothing(
            self, conn, db_path, tmp_path):
        module = _write_schema_module(str(tmp_path))
        plan = SM.handle_schema_plan(ns(
            module_path=module, db_path=db_path, src_root=None))
        applied = SM.handle_schema_apply(ns(
            migration_id=plan["migration_id"], db_path=db_path))
        assert applied["result"] == "applied", applied
        clean = SM.handle_schema_drift(ns(
            module_path=module, db_path=db_path))
        assert clean["result"] == "no_drift", clean
        assert clean["findings"] == []
        assert clean["finding_count"] == 0
        init_path = os.path.join(module, "init_db.py")
        with open(init_path, "r") as handle:
            content = handle.read()
        content = content.replace(
            "status      TEXT DEFAULT 'active',",
            "status      TEXT DEFAULT 'active',\n"
            "                    extra_note  TEXT DEFAULT '',")
        with open(init_path, "w") as handle:
            handle.write(content)
        before = _snapshot(conn, db_path)
        ledgers_before = {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry", "payment_ledger_entry")}
        result = SM.handle_schema_drift(ns(
            module_path=module, db_path=db_path))
        assert result["result"] == "drift_detected", result
        assert result["finding_count"] == 1
        assert len(result["findings"]) == 1
        finding = result["findings"][0]
        assert finding["type"] == "missing_column"
        assert finding["table"] == "m485_widget"
        assert finding["column"] == "extra_note"
        assert finding["details"] == (
            "Column 'extra_note' declared in init_db.py "
            "but missing from DB table 'm485_widget'")
        assert _snapshot(conn, db_path) == before
        assert {t: _count(conn, t, db_path) for t in (
            "gl_entry", "stock_ledger_entry",
            "payment_ledger_entry")} == ledgers_before

    def test_refuses_missing_path_and_writes_nothing(
            self, conn, db_path):
        before = _snapshot(conn, db_path)
        result = SM.handle_schema_drift(ns(
            module_path=None, db_path=db_path))
        assert "error" in result
        assert "--module-path is required" in result["error"]
        assert _snapshot(conn, db_path) == before
