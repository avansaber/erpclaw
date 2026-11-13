"""The release rule's lint: every core and first-party plugin
migration is transactional and has no escape. Every migration file is found by walking the source trees, so a new one
is linted the day it is added; each rule is proven to fire on a planted migration (a string, or a file under pytest's
tmp_path, never in the repo's migration folders), and never on a comment, a docstring or a quoted literal."""
from __future__ import annotations

import textwrap

import pytest

from migration_rules import (POPULATED_BOOKS_REFUSALS, REPO, RULES, lint_file, lint_source, migration_files,
                             sanitize, versions_folders)

HEADER = '''"""A planted migration."""
from alembic import op
import sqlalchemy as sa

revision = "planted"
down_revision = None
branch_labels = None


def upgrade() -> None:
'''


def planted(body: str) -> str:
    return HEADER + textwrap.indent(textwrap.dedent(body).strip("\n"), "    ") + "\n"


def rules(source: str) -> set[str]:
    return {f.rule for f in lint_source(source)}


# --- discovery ---------------------------------------------------------------------------------------------------

def test_discovery_finds_core_and_every_first_party_plugin_branch():
    folders = {str(p.relative_to(REPO)) for p in versions_folders()}
    assert "packages/erpclaw-core/src/erpclaw_core/migrations/versions" in folders
    assert "plugins/erpclaw-sales/src/erpclaw_sales/migrations/versions" in folders
    assert "plugins/erpclaw-stock/src/erpclaw_stock/migrations/versions" in folders
    names = {p.name for p in migration_files()}
    assert {"0001_core_books_baseline.py", "sales_0001_sales_tables.py", "stock_0001_stock_tables.py"} <= names


def test_discovery_finds_a_new_branch_the_day_it_is_added(tmp_path):
    versions = tmp_path / "plugins" / "erpclaw-new" / "src" / "erpclaw_new" / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "new_0001_tables.py").write_text(planted('op.execute("COMMIT")'))
    (tmp_path / "packages").mkdir()
    assert versions_folders(tmp_path) == [versions]
    [path] = migration_files(tmp_path)
    assert [f.rule for f in lint_file(path, tmp_path)] == ["commit"]


# --- every rule fires ----------------------------------------------------------------------------------------------

BAD = {
    "populated_books_refusal": [
        'op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM company) THEN RAISE EXCEPTION '
        "'populated books cannot upgrade'; END IF; END $$\")",
        'op.execute("DO $$ BEGIN IF (SELECT count(*) FROM posting) > 0 THEN RAISE EXCEPTION '
        "'start again'; END IF; END $$\")",
        'if op.get_bind().execute(sa.text("SELECT count(*) FROM company")).scalar_one():\n'
        '    raise RuntimeError("start again")',
        'populated = op.get_bind().execute(sa.text("SELECT 1 FROM company LIMIT 1")).first()\n'
        'if populated:\n    raise ValueError("books cannot upgrade")',
        'assert not op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM company)")).scalar()',
        'if op.get_bind().execute(sa.text("SELECT count(*) " + "FROM company")).scalar():\n'
        '    raise RuntimeError("start again")',
    ],
    "commit": [
        'op.execute("COMMIT")',
        'op.execute("commit;")',
        'op.execute("COM" + "MIT")',
        'op.execute("CREATE TABLE t (id int); END")',
        'op.execute("END TRANSACTION")',
        'op.execute("END WORK AND NO CHAIN")',
        'op.execute("PREPARE TRANSACTION \'x\'")',
        'op.get_bind().commit()',
        'finish = op.get_bind().commit\nfinish()',
        'bind = op.get_bind()\nbind.execute(sa.text("SELECT 1"))\nbind.commit()',
        'op.execute("DO $$ BEGIN INSERT INTO t VALUES (1); COMMIT; END $$")',
        'op.execute(f"DO $body$ BEGIN PERFORM {1}; Commit; END $body$")',
    ],
    "rollback": [
        'op.execute("ROLLBACK")',
        'op.execute("rollback to savepoint s")',
        'op.execute("ABORT")',
        'op.execute("ABORT AND CHAIN")',
        'op.get_bind().rollback()',
    ],
    "autocommit_block": [
        'with op.get_context().autocommit_block():\n    op.execute("SELECT 1")',
        'ctx = op.get_context()\nwith ctx.autocommit_block():\n    pass',
        'op.get_bind().execution_options(isolation_level="AUTOCOMMIT")',
        'op.get_bind().connection.autocommit = True',
        'finish = op.get_context().autocommit_block\nwith finish():\n    pass',
    ],
    "concurrently": [
        'op.execute("CREATE INDEX CONCURRENTLY ix ON t (a)")',
        'op.execute("drop index concurrently ix")',
        'op.create_index("ix", "t", ["a"], postgresql_concurrently=True)',
    ],
    "vacuum": ['op.execute("VACUUM ANALYZE t")', 'op.execute("vacuum")'],
    "alter_system": ['op.execute("ALTER SYSTEM SET work_mem = \'64MB\'")', 'op.execute("alter  system reset all")'],
    "alter_type_add_value": [
        'op.execute("ALTER TYPE status ADD VALUE \'archived\'")',
        'op.execute("alter type public.status\\n    add value if not exists \'x\' after \'y\'")',
    ],
    "dblink": [
        'op.execute("SELECT dblink_exec(\'dbname=other\', \'INSERT INTO t VALUES (1)\')")',
        'op.execute("CREATE EXTENSION dblink")',
        'op.execute("CREATE EXTENSION postgres_fdw")',
    ],
    "second_connection": [
        'from sqlalchemy import create_engine\neng = create_engine("postgresql://x")',
        'eng = sa.create_engine("postgresql://x")',
        'with op.get_bind().engine.connect() as c:\n    c.execute(sa.text("SELECT 1"))',
        'with op.get_bind().engine.begin() as c:\n    pass',
        'import psycopg\npsycopg.connect("dbname=x")',
        'raw = op.get_bind().engine.raw_connection()',
        'conn = connect()',
        'from sqlalchemy import create_engine as make\nmake("postgresql://x")',
        'import sqlalchemy as sql\nmake = sql.create_engine\nmake("postgresql://x")',
    ],
    "file_write": [
        'open("/tmp/x", "w").write("x")',
        'with open("/tmp/x", mode="a") as f:\n    f.write("x")',
        'import io\nio.open("/tmp/x", "wb")',
        'from pathlib import Path\nPath("/tmp/x").write_text("x")',
        'from pathlib import Path\nwith Path("/tmp/x").open("w") as f:\n    pass',
        'import os\nos.remove("/tmp/x")',
        'import os as filesystem\nfilesystem.remove("/tmp/x")',
        'from os import remove as discard\ndiscard("/tmp/x")',
        'from io import open as write_file\nwrite_file("/tmp/x", "w")',
        'from pathlib import Path\nPath("/tmp/x").rename("/tmp/y")',
        'from pathlib import Path\nPath("/tmp/x").replace("/tmp/y")',
        'import shutil\nshutil.copy("a", "b")',
        'mode = "r"\nopen("/tmp/x", mode)',  # a mode the source cannot show is refused
        'op.execute("COPY t TO \'/tmp/t.csv\'")',
        'op.execute("COPY (SELECT 1) TO PROGRAM \'curl x\'")',
        'op.execute("SELECT lo_export(1, \'/tmp/x\')")',
    ],
    "network": [
        'import requests\nrequests.post("https://example.invalid", data="x")',
        'import urllib.request\nurllib.request.urlopen("https://example.invalid")',
        'from urllib import request',
        'import socket\nsocket.create_connection(("example.invalid", 80))',
        'import importlib\nimportlib.import_module("socket")',
    ],
    "process": ['import subprocess\nsubprocess.run(["true"])', 'import os\nos.system("true")',
                'import os as operating_system\noperating_system.system("true")',
                'from os import system as run\nrun("true")'],
}


def test_every_rule_has_a_planted_case():
    assert set(BAD) == set(RULES)


@pytest.mark.parametrize("rule,body", [(r, b) for r, bodies in BAD.items() for b in bodies],
                         ids=[f"{r}-{i}" for r, bodies in BAD.items() for i in range(len(bodies))])
def test_each_rule_fires_on_a_planted_migration(rule, body):
    assert rule in rules(planted(body)), lint_source(planted(body))


def test_a_planted_file_under_tmp_path_fails_with_its_line(tmp_path):
    path = tmp_path / "0099_planted.py"
    path.write_text(planted('op.execute("CREATE TABLE t (id int)")\nop.execute("COMMIT")'))
    [finding] = lint_file(path, tmp_path)
    assert (finding.path, finding.rule) == ("0099_planted.py", "commit")
    assert path.read_text().splitlines()[finding.line - 1].strip() == 'op.execute("COMMIT")'


def test_the_line_inside_a_multiline_string_is_reported():
    source = planted('op.execute("""\n    CREATE TABLE t (id int);\n    VACUUM t\n""")')
    [finding] = lint_source(source)
    assert finding.rule == "vacuum" and source.splitlines()[finding.line - 1].strip() == "VACUUM t"


# --- and never on a comment, a docstring or a quoted literal -----------------------------------------------------

CLEAN = [
    '"""SELECT 1 FROM company"""\nraise RuntimeError("unavailable platform")',
    'op.execute("CREATE FUNCTION guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN '
    "IF EXISTS (SELECT 1 FROM company) THEN RAISE EXCEPTION 'runtime guard'; END IF; RETURN NEW; END $$\")",
    'op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM outbox_attachment WHERE file_id IS NULL) '
    "THEN RAISE EXCEPTION 'invalid historical row'; END IF; END $$\")",
    'if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM company)")).scalar():\n'
    '    op.execute("INSERT INTO books_identity VALUES (1)")',
    '# COMMIT; ROLLBACK; VACUUM; create_engine(); open("x", "w")\nop.execute("SELECT 1")',
    '"""This revision used to COMMIT and VACUUM, and called create_engine; no more."""',
    'op.execute("SELECT 1 -- then COMMIT\\n")',
    'op.execute("SELECT 1 /* ROLLBACK /* nested */ VACUUM */")',
    'sa.CheckConstraint("status in (\'committed\', \'rolled back\', \'rollback\', \'commit\')", name="ck")',
    'op.execute(\'CREATE TABLE "commit" ("vacuum" int)\')',
    'op.execute("ALTER TABLE t ADD COLUMN committed_at timestamptz, ADD COLUMN rollback_reason text")',
    'op.execute("CREATE TEMP TABLE scratch (id int) ON COMMIT DROP")',
    'op.execute("CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.a THEN RAISE EXCEPTION '
    '\'commit refused\'; END IF; RETURN NEW; END $$")',
    'op.execute(f"CREATE FUNCTION {1}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {2} END $$")',
    'op.execute("UPDATE t SET a = CASE WHEN b THEN 1 ELSE 2 END")',
    'with open("/etc/hostname") as f:\n    f.read()',
    'from pathlib import Path\nPath("x").read_text()',
    'statement = "SELECT x".replace("x", "1")\nop.execute(statement)',
    'op.create_index("ix", "t", ["a"], postgresql_concurrently=False)',
    'bind = op.get_bind()\nbind.execute(sa.text("SELECT quote_ident(current_schema())")).scalar_one()',
    'with op.get_bind().begin_nested():\n    op.execute("SELECT 1")',
    'op.execute("SELECT \'it\'\'s COMMIT\'")',
    'op.execute("SELECT E\'\\\\\' VACUUM\'")',
    'op.execute("CHECK (qty ~ \'^(0|[1-9][0-9]*)(\\\\.[0-9]+)?$\')")',
]


@pytest.mark.parametrize("body", CLEAN, ids=[f"clean-{i}" for i in range(len(CLEAN))])
def test_comments_docstrings_and_literals_never_fire(body):
    assert lint_source(planted(body)) == []


def test_an_unterminated_quote_fails_closed():
    assert "commit" in rules(planted('op.execute("SELECT \'unfinished; COMMIT")'))
    assert sanitize("SELECT 'x")[2] is False


def test_reviewed_population_refusals_exempt_only_that_rule():
    for revision, refusal in POPULATED_BOOKS_REFUSALS.items():
        assert refusal.previous and refusal.message and refusal.reason
        body = BAD["populated_books_refusal"][0]
        source = planted(body).replace('revision = "planted"', f'revision = "{revision}"')
        assert lint_source(source) == []
        assert "populated_books_refusal" in rules(source.replace(f'revision = "{revision}"',
                                                                 'revision = "new_revision"'))
        assert "commit" in rules(source + '    op.execute("COMMIT")\n')


@pytest.mark.parametrize("revision", POPULATED_BOOKS_REFUSALS)
def test_a_new_file_with_the_reviewed_guard_fails_lint(tmp_path, revision):
    [path] = [p for p in migration_files() if p.name.startswith(revision + "_")]
    copied = tmp_path / "new_revision.py"
    copied.write_text(path.read_text().replace(f'revision = "{revision}"', 'revision = "new_revision"'))
    findings = lint_file(copied, tmp_path)
    assert {f.rule for f in findings} == {"populated_books_refusal"}
    assert all(f.path == copied.name and f.line > 0 for f in findings)


# --- the real migrations -----------------------------------------------------------------------------------------

REAL = migration_files()


def _real_params():
    for path in REAL:
        yield pytest.param(path, id=path.relative_to(REPO).as_posix())


@pytest.mark.parametrize("path", list(_real_params()))
def test_each_real_migration_is_transactional_with_no_escape(path):
    findings = lint_file(path)
    assert not findings, "\n".join(str(f) for f in findings)
