"""The release rule's rollback tests (every migration is transactional with no escape): at every migration boundary of
core's chain and of each first-party plugin's chain, a failure injected right after that migration, inside the one
transaction the whole chain runs in, leaves the books exactly as before: their catalog dump and every table's rows are
equal byte for byte. Success alone proves nothing; this proves the failure path.

The comparison covers every non-system schema's tables, columns, defaults, constraints, indexes, triggers, functions,
types (enum labels and domains), views, sequences' definitions, rules, policies, comments, privileges, inheritance,
extensions, event triggers and the database's settings, plus every table's rows. It excludes **PostgreSQL's
sequence counters** (a sequence's last_value and is_called): `nextval` is not transactional, so a rolled-back insert
still advances a counter that existed before; the comparison excludes them and says so. A sequence a rolled-back migration
created is gone with it, and its definition is compared.

The migrations run through the engine's own runner (`erpclaw_core.migrate.upgrade`, `migrations/env.py`) on a
connection the test has already put in a transaction: Alembic then sees an external transaction and opens none of its
own, so the chain, core's and a plugin's, runs in the caller's one transaction.
"""
from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from conftest import drop_everything, empty_books
from erpclaw_core import migrate
from erpclaw_core.plugins import REGISTRY, load_plugins
from migration_rules import POPULATED_BOOKS_REFUSALS, migration_files, versions_folders

load_plugins()


class InjectedFailure(Exception):
    """The failure injected right after a migration: the transaction must roll back on it."""


# --- the chains ----------------------------------------------------------------------------------------------------

def _chain(versions) -> list[str]:
    """One branch's revisions, oldest first (each branch is linear)."""
    scripts = migrate.branch_scripts(versions)
    assert scripts, f"{versions}: no revisions"
    assert all(s._versioned_down_revisions == ((scripts[i + 1].revision,) if i + 1 < len(scripts) else ())
               for i, s in enumerate(scripts)), f"{versions}: expected one linear chain"
    return [s.revision for s in reversed(scripts)]


CORE = _chain(migrate.CORE_VERSIONS)
PLUGINS = {name: _chain(p.migrations) for name, p in sorted(REGISTRY.plugins.items()) if p.migrations}


def _boundaries():
    """(branch, revision, previous revision on its branch, or None for the branch's base)."""
    for i, rev in enumerate(CORE):
        yield "core", rev, CORE[i - 1] if i else None
    for name, chain in PLUGINS.items():
        for i, rev in enumerate(chain):
            yield name, rev, chain[i - 1] if i else None


BOUNDARIES = [pytest.param(*b, id=f"{b[0]}-{b[1]}") for b in _boundaries()]
POPULATED_UPGRADE_BOUNDARIES = [pytest.param(*b, id=f"{b[0]}-{b[1]}") for b in _boundaries()
                                if b[1] not in POPULATED_BOOKS_REFUSALS]


def test_the_chains_cover_every_migration_the_lint_reads():
    """Every discovered versions folder is core's or a loaded first-party plugin's, and every migration file the lint
    reads is a revision tested here, so the two halves of the rule never drift apart."""
    registered = {migrate.CORE_VERSIONS.resolve()} | {p.migrations.resolve() for p in REGISTRY.plugins.values()
                                                      if p.migrations}
    assert {f.resolve() for f in versions_folders()} == registered
    tested = set(CORE) | {r for chain in PLUGINS.values() for r in chain}
    scripts = [script for folder in registered for script in migrate.branch_scripts(folder)]
    assert len(tested) == len(scripts), "revision identifiers must be unique across branches"
    assert {p.resolve() for p in migration_files()} == {Path(s.path).resolve() for s in scripts}
    assert {"sales", "stock"} <= set(PLUGINS)
    assert set(POPULATED_BOOKS_REFUSALS) <= tested, "a reviewed refusal must name a real revision"
    for script in scripts:
        if refusal := POPULATED_BOOKS_REFUSALS.get(script.revision):
            assert script.down_revision == refusal.previous


# --- the dump ------------------------------------------------------------------------------------------------------

_USER_NS = ("n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%' "
            "AND n.nspname NOT LIKE 'pg_temp_%%'")

CATALOG = {
    "schema": f"SELECT n.nspname, pg_get_userbyid(n.nspowner), n.nspacl::text FROM pg_namespace n WHERE {_USER_NS}",
    "relation": f"""SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner), c.relpersistence,
        c.relacl::text, c.reloptions::text, c.relrowsecurity, c.relforcerowsecurity, c.relreplident, c.relispartition,
        obj_description(c.oid, 'pg_class')
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {_USER_NS}""",
    "column": f"""SELECT n.nspname, c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
        pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated, a.attisdropped, co.collname, a.attacl::text,
        col_description(c.oid, a.attnum)
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        LEFT JOIN pg_collation co ON co.oid = a.attcollation AND a.attcollation <> 0
        WHERE {_USER_NS} AND a.attnum > 0""",
    "constraint": f"""SELECT n.nspname, coalesce(c.relname, t.typname), k.conname, k.contype,
        pg_get_constraintdef(k.oid), k.condeferrable, k.condeferred, k.convalidated
        FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace
        LEFT JOIN pg_class c ON c.oid = k.conrelid LEFT JOIN pg_type t ON t.oid = k.contypid WHERE {_USER_NS}""",
    "index": f"""SELECT n.nspname, i.relname, pg_get_indexdef(x.indexrelid), x.indisvalid, x.indisready
        FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_namespace n ON n.oid = i.relnamespace
        WHERE {_USER_NS}""",
    "trigger": f"""SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid), t.tgenabled
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE {_USER_NS} AND NOT t.tgisinternal""",
    "function": f"""SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), p.prokind,
        pg_get_userbyid(p.proowner), p.proacl::text,
        CASE WHEN p.prokind IN ('f', 'p', 'w') THEN pg_get_functiondef(p.oid) END,
        obj_description(p.oid, 'pg_proc')
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE {_USER_NS}""",
    "type": f"""SELECT n.nspname, t.typname, t.typtype, pg_get_userbyid(t.typowner), t.typacl::text,
        format_type(t.typbasetype, t.typtypmod), t.typnotnull, t.typdefault,
        (SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid)
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE {_USER_NS}""",
    "view": f"""SELECT n.nspname, c.relname, pg_get_viewdef(c.oid)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {_USER_NS} AND c.relkind IN ('v', 'm')""",
    # a sequence's definition, never its counter (last_value, is_called): see the module's docstring
    "sequence": f"""SELECT n.nspname, c.relname, format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement, s.seqmax,
        s.seqmin, s.seqcache, s.seqcycle
        FROM pg_sequence s JOIN pg_class c ON c.oid = s.seqrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE {_USER_NS}""",
    "sequence_owner": f"""SELECT n.nspname, c.relname, tn.nspname, t.relname, a.attname, d.deptype
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a', 'i')
        JOIN pg_class t ON d.refclassid = 'pg_class'::regclass AND t.oid = d.refobjid
        JOIN pg_namespace tn ON tn.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
        WHERE {_USER_NS} AND c.relkind = 'S'""",
    "rule": f"""SELECT n.nspname, c.relname, r.rulename, pg_get_ruledef(r.oid)
        FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE {_USER_NS} AND r.rulename <> '_RETURN'""",
    "policy": f"""SELECT n.nspname, c.relname, p.polname, p.polcmd, p.polpermissive, p.polroles::text,
        pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
        FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE {_USER_NS}""",
    "inherits": f"""SELECT n.nspname, c.relname, pc.relname, i.inhseqno
        FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class pc ON pc.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {_USER_NS}""",
    "default_acl": """SELECT pg_get_userbyid(d.defaclrole), d.defaclnamespace::regnamespace::text, d.defaclobjtype,
        d.defaclacl::text FROM pg_default_acl d""",
    "extension": """SELECT e.extname, e.extversion, e.extnamespace::regnamespace::text FROM pg_extension e""",
    "event_trigger": """SELECT e.evtname, e.evtevent, e.evtenabled, e.evtfoid::regproc::text FROM pg_event_trigger e""",
    "database": """SELECT pg_get_userbyid(d.datdba), d.datacl::text, d.datconnlimit,
        (SELECT string_agg(s.setconfig::text, ';' ORDER BY s.setrole) FROM pg_db_role_setting s
         WHERE s.setdatabase = d.oid)
        FROM pg_database d WHERE d.datname = current_database()""",
    "large_object": "SELECT oid, pg_get_userbyid(lomowner), lomacl::text FROM pg_largeobject_metadata",
    "large_object_data": "SELECT loid, pageno, encode(data, 'hex') FROM pg_largeobject",
}


def dump(conn) -> str:
    """The books' catalog and every table's rows as one text, each section's lines sorted (code-point order, so no
    collation decides it), sequence counters excluded."""
    lines = []
    for section, query in CATALOG.items():
        rows = sorted("\t".join("\\N" if v is None else str(v) for v in row) for row in conn.execute(text(query)))
        lines += [f"{section}\t{r}" for r in rows]
    tables = conn.execute(text(f"""SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE {_USER_NS} AND c.relkind IN ('r', 'p', 'm')""")).scalars().all()
    for table in sorted(tables):
        rows = sorted(conn.execute(text(f"SELECT t::text FROM {table} t")).scalars())
        lines.append(f"rows\t{table}\t{len(rows)}")
        lines += [f"row\t{table}\t{r}" for r in rows]
    return "\n".join(lines) + "\n"


def books_dump(engine) -> bytes:
    with engine.connect() as conn:
        return dump(conn).encode("utf-8")


def assert_same(before: bytes, after: bytes, where: str) -> None:
    if before == after:
        return
    diff = list(difflib.unified_diff(before.decode().splitlines(), after.decode().splitlines(), "before", "after",
                                     lineterm="", n=0))
    pytest.fail(f"{where}: the books are not as before (sha256 {hashlib.sha256(before).hexdigest()[:12]} -> "
                f"{hashlib.sha256(after).hexdigest()[:12]}); residue:\n" + "\n".join(diff[:80]))


# --- the books -----------------------------------------------------------------------------------------------------

@pytest.fixture
def books():
    """Empty books on the suite's PostgreSQL test database, emptied again afterwards."""
    eng = empty_books()
    try:
        yield eng
    finally:
        try:
            drop_everything(eng)
        finally:
            eng.dispose()


def _target(branch: str, revision: str) -> list[str]:
    """The upgrades that take empty books through `revision`: a plugin's chain runs after core's head, as the update
    path runs it."""
    return [revision] if branch == "core" else [CORE[-1], revision]


def _previous(branch: str, previous: str | None) -> list[str]:
    return ([] if branch == "core" else [CORE[-1]]) + ([previous] if previous else [])


def _prepare_previous(conn, branch: str, previous: str | None, *, populated: bool = True) -> list[str]:
    steps = _previous(branch, previous)
    for step in steps:
        migrate.upgrade(conn, step)
    if not steps or not populated:
        return steps
    # Use the historical schema, never today's models, to exercise actual data
    # backfills as well as DDL. Keep these rows valid at every predecessor.
    conn.exec_driver_sql("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                         "VALUES ('release-company', 'Release books', 'USD', 1, '2026-01-01T00:00:00Z')")
    party_columns = {c["name"] for c in inspect(conn).get_columns("party")}
    key_column, key_value = (", city_key", ", 'chicago'") if "city_key" in party_columns else ("", "")
    conn.exec_driver_sql(f"INSERT INTO party (id, company_id, kind, name, city{key_column}) "
                         f"VALUES ('release-party', 'release-company', 'customer', 'Release customer', "
                         f"' ＣＨＩＣＡＧＯ '{key_value})")
    account_columns = {c["name"] for c in inspect(conn).get_columns("account")}
    side_column, side_value = (", normal_side", ", 'debit'") if "normal_side" in account_columns else ("", "")
    conn.exec_driver_sql(f"INSERT INTO account (id, company_id, number, name, root_type, is_group, frozen{side_column}) "
                         f"VALUES ('release-account', 'release-company', '1000', 'Release asset', "
                         f"'asset', false, false{side_value})")
    return steps


def _fail_after(engine, steps: list[str], revision: str) -> None:
    """Run `steps` through the runner in one transaction, check `revision` was applied, then fail."""
    with pytest.raises(InjectedFailure):
        with engine.connect() as conn, conn.begin():
            for step in steps:
                migrate.upgrade(conn, step)
            assert conn.in_transaction(), "the runner ended the caller's transaction"
            assert revision in migrate.applied_revisions(conn), f"{revision} did not run"
            raise InjectedFailure(revision)


# --- the rule ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("branch,revision,previous", BOUNDARIES)
def test_each_migration_commits_at_its_boundary_and_reopens(books, branch, revision, previous):
    """Populated predecessors must upgrade, except a reviewed refusal tested separately below;
    that boundary must still commit successfully from an empty predecessor."""
    with books.begin() as conn:
        _prepare_previous(conn, branch, previous, populated=revision not in POPULATED_BOOKS_REFUSALS)
    with books.begin() as conn:
        migrate.upgrade(conn, revision)
        assert revision in migrate.applied_revisions(conn)
        if branch == "core" and revision == "0005":
            assert conn.exec_driver_sql("SELECT count(*) FROM principal_grant").scalar_one() > 0
        if branch == "core" and revision == "0006":
            assert conn.exec_driver_sql("SELECT count(*) FROM books_identity").scalar_one() == 1
        if branch == "core" and revision == "0008":
            assert conn.exec_driver_sql("SELECT normal_side FROM account").scalar_one() == "debit"
        if branch == "core" and revision == "0012":
            assert conn.exec_driver_sql("SELECT city_key FROM party").scalar_one() == "chicago"
        if branch == "core" and revision == "0014":
            assert conn.exec_driver_sql("SELECT status FROM company").scalar_one() == "ready"
    # A separate connection sees the committed revision. Retrying it changes nothing.
    before = books_dump(books)
    with books.begin() as conn:
        assert revision in migrate.applied_revisions(conn)
        migrate.upgrade(conn, revision)
    assert_same(before, books_dump(books), f"{branch} {revision}, retry after commit")


@pytest.mark.parametrize("fail", [False, True], ids=["commit", "rollback-after-checks"])
def test_complete_release_chain_and_integrity_checks_share_one_transaction(books, fail):
    from erpclaw_core.backup import _integrity
    from erpclaw_core.plugins import check_books

    before = books_dump(books)
    try:
        with books.begin() as conn:
            migrate.upgrade(conn, CORE[-1])
            for chain in PLUGINS.values():
                migrate.upgrade(conn, chain[-1])
            assert migrate.applied_revisions(conn) == {CORE[-1]} | {c[-1] for c in PLUGINS.values()}
            check_books(conn)
            _integrity(conn)
            if fail:
                raise InjectedFailure("after integrity checks")
    except InjectedFailure:
        if not fail:
            raise
    if fail:
        assert_same(before, books_dump(books), "complete release chain, after integrity checks")
    else:
        with books.connect() as conn:
            check_books(conn)
            _integrity(conn)
            assert migrate.applied_revisions(conn) == {CORE[-1]} | {c[-1] for c in PLUGINS.values()}


@pytest.mark.parametrize("branch,revision,previous", BOUNDARIES)
def test_a_failure_after_each_migration_from_empty_books_leaves_them_empty(books, branch, revision, previous):
    """The whole chain from empty books, core's then the plugin's, in one transaction; a failure right after this
    migration; the books equal their dump from before, byte for byte."""
    before = books_dump(books)
    _fail_after(books, _target(branch, revision), revision)
    assert_same(before, books_dump(books), f"{branch} {revision}, from empty books")


@pytest.mark.parametrize("branch,revision,previous", POPULATED_UPGRADE_BOUNDARIES)
def test_a_failure_after_each_migration_on_books_at_the_previous_revision_leaves_them_as_they_were(
        books, branch, revision, previous):
    """The books committed at the revision before this one (core's head for a plugin's base): their schema, the
    business rows, currencies and version rows are in the dump. This migration in one transaction, a failure right after it, and
    the books equal their dump from before, byte for byte."""
    with books.begin() as conn:
        committed = _prepare_previous(conn, branch, previous)
    before = books_dump(books)
    assert committed == [] or before.count(b"\nrow\t") > 0, "the books at the previous revision hold rows"
    try:
        _fail_after(books, [revision], revision)
    finally:
        # An unreviewed refusal before the injected failure still fails the test
        # and must leave no residue.
        assert_same(before, books_dump(books), f"{branch} {revision}, from books at {committed[-1] if committed else 'empty'}")


@pytest.mark.parametrize("revision,refusal", list(POPULATED_BOOKS_REFUSALS.items()))
def test_reviewed_boundary_refuses_populated_books_with_reason_and_no_changes(books, revision, refusal):
    branch = next(branch for branch, rev, _ in _boundaries() if rev == revision)
    with books.begin() as conn:
        _prepare_previous(conn, branch, refusal.previous)
        assert refusal.previous in migrate.applied_revisions(conn)
        assert conn.exec_driver_sql("SELECT count(*) FROM company").scalar_one() > 0
    before = books_dump(books)
    try:
        with pytest.raises(DBAPIError) as raised:
            with books.begin() as conn:
                migrate.upgrade(conn, revision)
        assert raised.value.orig.diag.message_primary == refusal.message, refusal.reason
    finally:
        assert_same(before, books_dump(books), f"{revision}: {refusal.reason}")
    with books.connect() as conn:
        assert refusal.previous in migrate.applied_revisions(conn)
        assert revision not in migrate.applied_revisions(conn)


# --- the comparison sees residue -----------------------------------------------------------------------------------

RESIDUE = {
    "table": "CREATE TABLE residue (id int)",
    "column": "ALTER TABLE currency ADD COLUMN residue int",
    "default": "ALTER TABLE currency ALTER COLUMN name SET DEFAULT 'x'",
    "index": "CREATE INDEX ix_residue ON currency (name)",
    "constraint": "ALTER TABLE currency ADD CONSTRAINT ck_residue CHECK (code <> '')",
    "row": "INSERT INTO currency (code, name, minor_units) VALUES ('XXX', 'Residue', 2)",
    "row value": "UPDATE currency SET name = 'Residue' WHERE code = 'USD'",
    "type": "CREATE TYPE residue AS ENUM ('a')",
    "function": "CREATE FUNCTION residue() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "trigger": "CREATE TRIGGER residue BEFORE INSERT ON currency FOR EACH ROW EXECUTE FUNCTION erpclaw_refuse_change()",
    "sequence": "CREATE SEQUENCE residue_seq",
    "view": "CREATE VIEW residue_v AS SELECT 1 AS one",
    "privilege": "GRANT SELECT ON currency TO PUBLIC",
    "comment": "COMMENT ON TABLE currency IS 'residue'",
    "schema": "CREATE SCHEMA residue",
    "schema privilege": "GRANT CREATE ON SCHEMA public TO PUBLIC",
    "policy": "CREATE POLICY residue ON currency USING (true)",
    "rule": "CREATE RULE residue AS ON DELETE TO currency DO ALSO NOTIFY residue",
    "default privilege": "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO PUBLIC",
    "enum labels": "CREATE TYPE residue AS ENUM ('a', 'b')",
    "large object": "SELECT lo_create(0)",
}


@pytest.mark.parametrize("statement", list(RESIDUE.values()), ids=list(RESIDUE))
def test_the_comparison_sees_each_kind_of_residue(books, statement):
    """Inside a transaction that is rolled back afterwards: the dump differs once the residue is there."""
    with books.begin() as conn:
        migrate.upgrade(conn, CORE[-1])
    before = books_dump(books)
    with books.connect() as conn, conn.begin() as trans:
        conn.exec_driver_sql(statement)
        assert dump(conn).encode() != before
        trans.rollback()
    assert books_dump(books) == before


def test_the_comparison_excludes_sequence_counters_only(books):
    """A counter moved by nextval is not residue (PostgreSQL's sequences are not transactional);
    a changed definition is."""
    with books.begin() as conn:
        migrate.upgrade(conn, CORE[-1])
        conn.exec_driver_sql("CREATE SEQUENCE release_test_seq")
    try:
        before = books_dump(books)
        with books.connect() as conn, conn.begin() as trans:
            conn.exec_driver_sql("SELECT nextval('release_test_seq')")
            trans.rollback()
        with books.connect() as conn:
            assert conn.exec_driver_sql("SELECT last_value FROM release_test_seq").scalar() == 1
            assert conn.exec_driver_sql("SELECT is_called FROM release_test_seq").scalar() is True
        assert books_dump(books) == before
        with books.connect() as conn, conn.begin() as trans:
            conn.exec_driver_sql("ALTER SEQUENCE release_test_seq INCREMENT BY 2")
            assert dump(conn).encode() != before
            trans.rollback()
    finally:
        with books.begin() as conn:
            conn.exec_driver_sql("DROP SEQUENCE IF EXISTS release_test_seq")


def test_sequence_ownership_is_compared(books):
    with books.begin() as conn:
        migrate.upgrade(conn, CORE[-1])
        conn.exec_driver_sql("CREATE SEQUENCE release_owned_seq")
    try:
        before = books_dump(books)
        with books.connect() as conn, conn.begin() as trans:
            conn.exec_driver_sql("ALTER SEQUENCE release_owned_seq OWNED BY currency.minor_units")
            assert dump(conn).encode() != before
            trans.rollback()
        assert books_dump(books) == before
    finally:
        with books.begin() as conn:
            conn.exec_driver_sql("DROP SEQUENCE release_owned_seq")


def test_existing_large_object_data_is_compared(books):
    with books.begin() as conn:
        oid = conn.exec_driver_sql("SELECT lo_from_bytea(0, decode('0102', 'hex'))").scalar_one()
    try:
        before = books_dump(books)
        with books.connect() as conn, conn.begin() as trans:
            conn.execute(text("SELECT lo_put(:oid, 0, decode('0304', 'hex'))"), {"oid": oid})
            assert dump(conn).encode() != before
            trans.rollback()
        assert books_dump(books) == before
    finally:
        with books.begin() as conn:
            conn.execute(text("SELECT lo_unlink(:oid)"), {"oid": oid})


def test_a_migration_that_escapes_its_transaction_is_caught(books, monkeypatch):
    """The rule's own proof: a migration that commits mid-chain leaves residue the comparison reports. The base
    revision's upgrade is wrapped (in this process only, nothing on disk) to COMMIT after it runs."""
    from alembic.script import ScriptDirectory

    real = ScriptDirectory._load_revisions  # Alembic loads each revision's module afresh for each script directory

    def escaping(self):
        for script in real(self):
            if script.revision == CORE[0]:
                upgrade = script.module.upgrade

                def upgrade_and_commit(upgrade=upgrade):
                    from alembic import op

                    upgrade()
                    op.execute("COMMIT")

                script.module.upgrade = upgrade_and_commit
            yield script

    monkeypatch.setattr(ScriptDirectory, "_load_revisions", escaping)
    before = books_dump(books)
    try:
        _fail_after(books, [CORE[0]], CORE[0])  # the injected failure is raised and rolled back as for the others
    except Exception:  # noqa: BLE001 - or the runner or driver refuses after the COMMIT: residue is the point either way
        pass
    after = books_dump(books)
    assert after != before, "an escaping migration must leave residue the comparison sees"
    assert b"\tcurrency\t" in after, "the base revision's tables stayed"
