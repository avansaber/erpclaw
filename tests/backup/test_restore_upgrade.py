"""Restore at the backup's recorded revision, prove, then migrate: the
new database's schema is made at the recorded core and plugin revisions, the rows load by the backup's own column
lists, the book diff proves them equal to the backup at that revision, the migrations to head run in one transaction
with the integrity checks, and only then is the database published for the service to switch to. A backup from a
newer schema, or with a plugin revision outside this installation's chain, is refused with nothing published."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from backup_support import databases, nothing_published, older_books, open_target, pg_new, snapshot, upgradeable_books  # noqa: F401  (a fixture)
from erpclaw_core import backup as bk, backup_reflect as br, files, restore_upgrade as ru
from erpclaw_core.common import company_ctx
from erpclaw_core.db import make_engine
from erpclaw_core.migrate import applied_revisions, core_head
from erpclaw_core.plugins import check_installed
from test_backup_reflect import _tree, books  # noqa: F401 (fixture with currency scales, a reversal and a PDF)


PDF = b"%PDF-1.7\n" + b"a document the restore must carry\n" * 20


class Died(RuntimeError):
    """A failure injected between the steps."""


def _sha(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _rewrite(path: Path, table: str, change) -> None:
    """Edit one table file and recompute its hashes in the manifest, as whoever edits a backup can."""
    f = path / "tables" / f"{table}.jsonl"
    lines = change(f.read_text().splitlines())
    f.write_text("\n".join(lines) + ("\n" if lines else ""))
    manifest = json.loads((path / "MANIFEST.json").read_text())
    manifest["tables"][table].update(rows=len(lines), sha256=_sha(lines))
    (path / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _manifest(path: Path, change) -> None:
    manifest = json.loads((path / "MANIFEST.json").read_text())
    change(manifest)
    (path / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _hooked(monkeypatch, at: str | None = None, seen: dict | None = None):
    def hook(step, conn=None, **kw):
        if seen is not None and conn is not None:
            seen[step] = {"revisions": applied_revisions(conn), "tables": set(inspect(conn).get_table_names())}
        if step == at:
            raise Died(f"injected at {step}")
    monkeypatch.setattr(ru, "_HOOK", hook)


# --- an older backup ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("core", ["0004", "0007", "0009"])
@pytest.mark.parametrize("existing_target", [False, True])
def test_a_populated_older_backup_is_refused_without_changing_any_books(
        engine, other_database, tmp_path, pg_new, monkeypatch, core, existing_target):
    source = make_engine(other_database)
    try:
        older_books(source, core)
        original = snapshot(source)
        br.backup(source, tmp_path / "b", schema_release="3.0.0")
        live = snapshot(engine)
        untouched = _tree(tmp_path)
        all_databases = set(databases("%"))
        unused = pg_new()
        target = (bk.NewPostgresDatabase(engine.url.render_as_string(hide_password=False), engine.url.database)
                  if existing_target else unused)

        def no_staging(*args, **kwargs):
            pytest.fail("a populated pre-format-2 backup must be refused before staging")
        monkeypatch.setattr(ru, "_PostgresStage", no_staging)
        with pytest.raises(bk.BackupRefused) as refusal:
            ru.restore(tmp_path / "b", target)
        assert str(refusal.value) == ("This backup uses an older bookkeeping format and cannot be upgraded by "
                                     "this ERPClaw. Your current books have not been changed.")
        assert snapshot(source) == original
        assert snapshot(engine) == live  # includes the target's every row when it already exists
        assert set(databases("%")) == all_databases  # an absent target stays absent
        assert _tree(tmp_path) == untouched  # backup, live files and any unrelated files stay byte-identical
        nothing_published(unused)
    finally:
        source.dispose()


@pytest.mark.parametrize("core", ["0004", "0007", "0009", "0010"])
def test_an_empty_older_backup_is_proven_at_its_revision_and_upgraded(engine, tmp_path, pg_new, monkeypatch, core):
    older_books(engine, core, populated=False)
    before = snapshot(engine)
    br.backup(engine, tmp_path / "b", schema_release="3.0.0")
    seen: dict = {}
    _hooked(monkeypatch, seen=seen)
    target = pg_new()
    result = ru.restore(tmp_path / "b", target)
    assert result.location == target.name
    assert result.recorded == br.Revisions(core, {"sales": "sales_0001"})
    assert result.migrated == br.Revisions(core_head(), {"sales": br.chains()[1]["sales"][0]})
    # the proof ran on the books at the recorded revision, before any later migration
    assert seen["proved"]["revisions"] == {core, "sales_0001"}
    assert ("file_object" in seen["proved"]["tables"]) == (core >= "0010")
    assert ("books_identity" in seen["proved"]["tables"]) == (core >= "0006")
    assert seen["migrating"]["revisions"] == {core_head(), br.chains()[1]["sales"][0]}

    restored = open_target(target)
    try:
        check_installed(restored)
        with restored.connect() as conn:
            assert applied_revisions(conn) == {core_head(), br.chains()[1]["sales"][0]}
        after = snapshot(restored)
        assert after["currency"] == before["currency"]
        from erpclaw_core.db import OWNER_MARK

        assert after["erpclaw_books"] == [{"mark": OWNER_MARK}]
        assert len(after["books_identity"]) == 1
        assert all(rows == [] for name, rows in after.items()
                   if name not in ("alembic_version", "currency", "erpclaw_books", "books_identity"))
        again = bk.backup(restored, tmp_path / "after")
        assert again["core_revision"] == core_head()
        assert again["books_id"] == after["books_identity"][0]["books_id"]
    finally:
        restored.dispose()
    assert snapshot(engine) == before


def test_a_same_version_backup_restores_equal_to_its_books_with_its_files(engine, tmp_path, pg_new):
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="Stark Manufacturing")
        files.put(ctx, PDF, "application/pdf", ("sales_invoice", "inv-1"))
    br.backup(engine, tmp_path / "b")
    target = pg_new()
    result = ru.restore(tmp_path / "b", target)
    assert result.recorded == result.migrated
    restored = open_target(target)
    try:
        assert bk.book_diff(engine, restored) == []
        check_installed(restored)
        with restored.connect() as conn:
            rows = [dict(r) for r in conn.execute(text("SELECT * FROM file_object")).mappings()]
    finally:
        restored.dispose()
    assert len(rows) == 1
    assert files.check_restored(files.files_dir() / files.root_name(target.name), rows) == []


def test_an_engine_export_of_the_same_version_restores_too(engine, tmp_path, pg_new):
    """A backup the engine's own export wrote (no schema_version) is read by its own column lists all the same."""
    bk.backup(engine, tmp_path / "b")
    target = pg_new()
    ru.restore(tmp_path / "b", target)
    restored = open_target(target)
    try:
        assert bk.book_diff(engine, restored) == []
    finally:
        restored.dispose()


# --- refusals, nothing published ------------------------------------------------------------------------------------

@pytest.mark.parametrize("manifest", [None, b"not JSON", b"\xff"])
def test_a_missing_or_unreadable_manifest_has_a_path_free_refusal(tmp_path, pg_new, manifest):
    if manifest is not None:
        (tmp_path / "MANIFEST.json").write_bytes(manifest)
    target = pg_new()
    with pytest.raises(bk.BackupRefused) as refusal:
        ru.restore(tmp_path, target)
    message = str(refusal.value)
    assert message == "The backup has no readable manifest."
    assert "/" not in message and "\\" not in message
    nothing_published(target)


def test_a_backup_from_a_newer_schema_is_refused(engine, tmp_path, pg_new):
    br.backup(engine, tmp_path / "b")
    _rewrite(tmp_path / "b", "alembic_version",
             lambda lines: sorted(json.dumps({"version_num": "9999" if json.loads(x)["version_num"] == core_head()
                                              else json.loads(x)["version_num"]}) for x in lines))

    def newer(m):
        m["core_revision"] = m["schema_version"]["core_revision"] = "9999"
    _manifest(tmp_path / "b", newer)
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="made by a newer ERPClaw; update ERPClaw first"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_a_newer_backup_format_is_refused(engine, tmp_path, pg_new):
    br.backup(engine, tmp_path / "b")
    _manifest(tmp_path / "b", lambda m: m.update(format_version=2))
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="newer ERPClaw"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_a_plugin_revision_outside_the_installed_chain_is_refused(engine, tmp_path, pg_new):
    br.backup(engine, tmp_path / "b")
    _rewrite(tmp_path / "b", "alembic_version",
             lambda lines: sorted(x.replace(br.chains()[1]["sales"][0], "sales_0099") for x in lines))
    _manifest(tmp_path / "b", lambda m: m["schema_version"]["plugins"].update(sales="sales_0099"))
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="sales plugin is at a revision this ERPClaw"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_an_engine_export_with_a_revision_in_no_chain_is_refused(engine, tmp_path, pg_new):
    bk.backup(engine, tmp_path / "b")  # no schema_version: the rows alone say which revision is whose
    _rewrite(tmp_path / "b", "alembic_version",
             lambda lines: sorted(x.replace(br.chains()[1]["sales"][0], "sales_0099") for x in lines))
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="sales_0099"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_a_plugin_the_installation_lacks_is_refused(engine, tmp_path, pg_new):
    br.backup(engine, tmp_path / "b")
    ghost = {"name": "ghost", "version": "1.0.0", "kind": "connector", "installed_at": "2026-10-26T12:00:00.000000Z",
             "manifest_digest": "0" * 64, "artifact_digest": "0" * 64, "after_core_revision": "0001",
             "status": "installed"}
    _rewrite(tmp_path / "b", "plugin_install",
             lambda lines: lines + [json.dumps(ghost, separators=(",", ":"))])
    _manifest(tmp_path / "b", lambda m: m["plugins"].append(ghost))
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="ghost plugin, which this ERPClaw does not have"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_a_damaged_table_file_is_refused(engine, tmp_path, pg_new):
    br.backup(engine, tmp_path / "b")
    f = tmp_path / "b" / "tables" / "company.jsonl"
    f.write_text(f.read_text().replace("Stark", "Stork"))
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="damaged"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_rows_that_do_not_have_their_revisions_columns_are_refused(engine, tmp_path, pg_new):
    upgradeable_books(engine, "0011", "sales_0001")
    br.backup(engine, tmp_path / "b")
    _rewrite(tmp_path / "b", "company",  # a column 0011 does not have
             lambda lines: [json.dumps(dict(json.loads(x), setup_pending=False), separators=(",", ":")) for x in lines])
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="company"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_a_failure_during_the_migration_publishes_nothing(engine, tmp_path, pg_new, monkeypatch):
    upgradeable_books(engine, "0011", "sales_0001")
    br.backup(engine, tmp_path / "b")
    seen: dict = {}
    _hooked(monkeypatch, at="migrating", seen=seen)
    target = pg_new()
    with pytest.raises(Died):
        ru.restore(tmp_path / "b", target)
    assert seen["migrating"]["revisions"] == {core_head(), br.chains()[1]["sales"][0]}
    nothing_published(target)


def test_a_failure_after_the_migration_commits_publishes_nothing(engine, tmp_path, pg_new, monkeypatch):
    upgradeable_books(engine, "0011", "sales_0001")
    br.backup(engine, tmp_path / "b")
    _hooked(monkeypatch, at="migrated")
    target = pg_new()
    with pytest.raises(Died):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_the_module_takes_no_engine_and_reads_no_books_url():
    """It never opens books in use: no Engine parameter, no environment URL."""
    import inspect as pyinspect

    source = pyinspect.getsource(ru)
    assert "ERPCLAW_DATABASE_URL" not in source and "os.environ" not in source
    assert list(pyinspect.signature(ru.restore).parameters) == ["path", "target"]


def test_a_runtime_credential_cannot_stage_a_restore(engine, runtime_login, tmp_path, pg_new):
    br.backup(engine, tmp_path / "b")
    before = snapshot(engine)
    target = bk.NewPostgresDatabase(runtime_login, pg_new().name)
    with pytest.raises(bk.BackupRefused, match="owner role needs CREATEDB"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)
    assert snapshot(engine) == before


def test_upgrade_refreshes_install_records_as_owner_without_runtime_privileges(
        engine, runtime_login, tmp_path, pg_new, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import DBAPIError
    from erpclaw_core.grants import privilege_differences
    from erpclaw_core.plugins import REGISTRY

    upgradeable_books(engine, "0011", "sales_0001")
    before = snapshot(engine)
    br.backup(engine, tmp_path / "b")
    role = make_url(runtime_login).username
    target = pg_new(runtime_role=role)
    seen = []

    def hook(step, conn=None, **kw):
        if step not in ("proved", "migrating"):
            return
        user, owner = conn.exec_driver_sql(
            "SELECT current_user, pg_get_userbyid(datdba) FROM pg_database "
            "WHERE datname = current_database()").one()
        assert user == owner == engine.url.username
        assert user != role
        # The original digests must survive the initial insert and proof, before the refresh at head.
        assert snapshot(conn)["plugin_install"] == before["plugin_install"]
        seen.append(step)

    monkeypatch.setattr(ru, "_HOOK", hook)
    ru.restore(tmp_path / "b", target)
    assert seen == ["proved", "migrating"]
    restored = open_target(target)
    runtime = create_engine(make_url(runtime_login).set(database=target.name))
    try:
        check_installed(restored)
        rows = snapshot(restored)["plugin_install"]
        assert rows
        for row in rows:
            plugin = REGISTRY.plugins[row["name"]]
            assert (row["version"], row["manifest_digest"], row["artifact_digest"]) == (
                plugin.version, plugin.manifest.digest(), plugin.artifact_digest)
        with restored.connect() as conn:
            assert privilege_differences(conn, role) == []
        for column in ("version", "manifest_digest", "artifact_digest"):
            with runtime.begin() as conn:
                with pytest.raises(DBAPIError) as refusal:
                    conn.exec_driver_sql(f"UPDATE plugin_install SET {column} = {column}")
                assert refusal.value.orig.sqlstate == "42501"  # insufficient_privilege
                conn.rollback()
    finally:
        runtime.dispose()
        restored.dispose()
    assert snapshot(engine) == before


@pytest.mark.parametrize("core", list(reversed(br.chains()[0][:br.chains()[0].index("0011") + 1])))
@pytest.mark.parametrize("sales", br.chains()[1]["sales"])
def test_supported_revisions_preserve_every_row_and_amount(books, tmp_path, pg_new, monkeypatch, core, sales):
    upgradeable_books(books, core, sales)
    before = snapshot(books)
    assert before["posting"] and before["ledger_entry"] and before["sales_invoice_line"]
    assert {row["currency"] for row in before["ledger_entry"]} >= {"USD", "JPY", "KWD"}
    br.backup(books, tmp_path / "b")
    proved = []

    def hook(step, conn=None, **kw):
        if step == "proved":
            assert snapshot(conn) == before  # before migrating or invalidating authentication
            proved.append(True)
    monkeypatch.setattr(ru, "_HOOK", hook)
    target = pg_new()
    ru.restore(tmp_path / "b", target)
    assert proved == [True]
    restored = open_target(target)
    try:
        check_installed(restored)
        after = snapshot(restored)
        for name, rows in before.items():
            assert len(after[name]) == len(rows), name
            if name in ("alembic_version", "plugin_install"):
                continue
            # Compare every original column, including every amount, rate and quantity, without totals/rounding.
            assert [{k: row[k] for k in old} for old, row in zip(rows, after[name])] == rows, name
        for name in set(after) - set(before):
            assert after[name] == [], name
        if core < "0012":
            assert all(row["city_key"] == (row["city"].strip().casefold() if row["city"] else None)
                       for row in after["party"])
    finally:
        restored.dispose()
    assert snapshot(books) == before


@pytest.mark.parametrize("empty", [False, True])
def test_an_existing_database_is_never_overwritten(engine, tmp_path, pg_new, monkeypatch, empty):
    br.backup(engine, tmp_path / "b")
    before = snapshot(engine)
    target = bk.NewPostgresDatabase(engine.url.render_as_string(hide_password=False), engine.url.database)
    if empty:
        from sqlalchemy import create_engine
        target = pg_new()
        admin = create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'CREATE DATABASE "{target.name}"')
        finally:
            admin.dispose()
    staged = []
    monkeypatch.setattr(bk, "_HOOK", lambda step, **kw: staged.append(step))
    with pytest.raises(bk.BackupRefused) as refusal:
        ru.restore(tmp_path / "b", target)
    assert str(refusal.value) == f"database {target.name} already exists; restore never writes over anything"
    assert staged == []  # refusal comes from the initial guard, before a staging database is made
    assert databases("erpclaw\\_restore\\_%") == []
    assert snapshot(engine) == before
    if empty:
        restored = open_target(target)
        try:
            assert snapshot(restored) == {}
        finally:
            restored.dispose()


def test_authentication_is_proven_then_voided_before_publication(engine, tmp_path, pg_new, monkeypatch):
    from datetime import timedelta
    from sqlalchemy import select
    from erpclaw_core import models as m
    from test_principal_session_tables import AT, rows

    with engine.begin() as conn:
        data = rows(conn.execute(select(m.principal.c.id).limit(1)).scalar_one())
        conn.execute(m.principal_sign_in.insert().values(**data["principal_sign_in"]))
        conn.execute(m.principal_session.insert().values(**data["principal_session"]))
        conn.execute(m.principal_sign_in.insert().values(**dict(data["principal_sign_in"],
                     code_sha256="f" * 64, used_at=AT + timedelta(minutes=2))))
        conn.execute(m.principal_session.insert().values(**dict(data["principal_session"], id="closed",
                     secret_sha256="a" * 64, ended_at=AT + timedelta(hours=1), ended_why="disabled")))
    before = snapshot(engine)
    br.backup(engine, tmp_path / "b")
    seen = []

    def hook(step, conn=None, **kw):
        if step == "proved":
            assert snapshot(conn) == before
            seen.append(step)
        if step == "migrating":
            assert all(row["used_at"] for row in snapshot(conn)["principal_sign_in"])
            assert all(row["ended_at"] for row in snapshot(conn)["principal_session"])
            seen.append(step)
    monkeypatch.setattr(ru, "_HOOK", hook)
    target = pg_new()
    ru.restore(tmp_path / "b", target)
    assert seen == ["proved", "migrating"]
    restored = open_target(target)
    try:
        after = snapshot(restored)
        for name, old_rows in before.items():
            assert len(after[name]) == len(old_rows), name
            for old, new in zip(old_rows, after[name]):
                if name == "principal_sign_in" and old["used_at"] is None:
                    assert new["used_at"] is not None
                    new = dict(new, used_at=None)
                elif name == "principal_session" and old["ended_at"] is None:
                    assert new["ended_at"] is not None and new["ended_why"] == "restored"
                    new = dict(new, ended_at=None, ended_why=None)
                assert old == new, name
        assert bk.verify(tmp_path / "b", target) == []
    finally:
        restored.dispose()
    assert snapshot(engine) == before


@pytest.mark.parametrize("step", ["restored", "proved", "migrating", "migrated"])
def test_failure_at_each_restore_step_keeps_live_books(engine, tmp_path, pg_new, monkeypatch, step):
    before = snapshot(engine)
    br.backup(engine, tmp_path / "b")
    _hooked(monkeypatch, at=step)
    target = pg_new()
    with pytest.raises(Died):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)
    assert snapshot(engine) == before


def test_program_version_does_not_decide_schema_acceptance(engine, tmp_path, pg_new):
    br.backup(engine, tmp_path / "b")
    _manifest(tmp_path / "b", lambda m: m.update(engine_version="999.0.0", written_by={"version": "999.0.0"}))
    ru.restore(tmp_path / "b", pg_new())


@pytest.mark.parametrize("bad", [None, [], "bad", {"rows": True, "sha256": "bad"}])
def test_malformed_table_metadata_is_refused_cleanly(engine, tmp_path, pg_new, bad):
    br.backup(engine, tmp_path / "b")
    _manifest(tmp_path / "b", lambda m: m["tables"].update(company=bad))
    target = pg_new()
    with pytest.raises(bk.BackupRefused):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_corrupt_referenced_file_is_refused_without_publication(engine, tmp_path, pg_new):
    with engine.begin() as conn:
        files.put(company_ctx(conn, company_name="Stark Manufacturing"), PDF, "application/pdf",
                  ("sales_invoice", "inv-1"))
    br.backup(engine, tmp_path / "b")
    payload = next(p for p in (tmp_path / "b" / "files").rglob("*") if p.is_file())
    payload.write_bytes(b"corrupt")
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="files do not match"):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


@pytest.mark.parametrize("table,bad", [("company", "not json"), ("company", "[]"),
                                       ("company", "null"), ("currency", '{"unexpected":1}')])
def test_malformed_rows_are_refused_cleanly(engine, tmp_path, pg_new, table, bad):
    br.backup(engine, tmp_path / "b")
    _rewrite(tmp_path / "b", table, lambda lines: [bad])
    target = pg_new()
    with pytest.raises(bk.BackupRefused):
        ru.restore(tmp_path / "b", target)
    nothing_published(target)


def test_all_money_scales_survive_an_upgrade(books, tmp_path, pg_new):
    upgradeable_books(books)
    before = snapshot(books)
    assert {row["currency"] for row in before["ledger_entry"]} >= {"USD", "JPY", "KWD"}
    br.backup(books, tmp_path / "b")
    target = pg_new()
    ru.restore(tmp_path / "b", target)
    restored = open_target(target)
    try:
        after = snapshot(restored)
        for name in ("posting", "ledger_entry", "sales_invoice", "sales_invoice_line", "sales_payment",
                     "sales_payment_allocation", "item", "file_object"):
            expected = before[name]
            if name == "sales_invoice_line":
                expected = [dict(row, line_no=None, named_warehouse_id=None, item_kind=None) for row in expected]
            assert after[name] == expected, name
    finally:
        restored.dispose()


@pytest.mark.parametrize("table", ["erpclaw_books", "books_identity"])
def test_even_a_mark_or_identity_without_companies_refuses_the_format_boundary(engine, tmp_path, pg_new, table):
    from erpclaw_core.db import OWNER_MARK
    from erpclaw_core.common import new_id

    older_books(engine, "0010", populated=False)
    with engine.begin() as conn:
        if table == "erpclaw_books":
            conn.execute(text("INSERT INTO erpclaw_books VALUES (:mark)"), {"mark": OWNER_MARK})
        else:
            conn.execute(text("INSERT INTO books_identity VALUES (1, :id)"), {"id": new_id()})
    before = snapshot(engine)
    br.backup(engine, tmp_path / "b")
    target = pg_new()
    with pytest.raises(bk.BackupRefused, match="older bookkeeping format.*cannot be upgraded"):
        ru.restore(tmp_path / "b", target)
    assert snapshot(engine) == before
    nothing_published(target)


@pytest.mark.parametrize("stock_revision", br.chains()[1]["stock"])
def test_stock_history_and_amounts_survive_each_plugin_upgrade(engine, tmp_path, pg_new, stock_revision):
    from datetime import date
    from erpclaw_core import models as m
    from erpclaw_core.common import new_id
    from erpclaw_stock import service as stock

    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="Stark Manufacturing")
        item = new_id()
        conn.execute(m.item.insert().values(id=item, company_id=ctx.company_id, name="Stocked bolt",
                                           unit="unit", standard_rate="1"))
        warehouse = stock.create_warehouse(ctx, "Main warehouse")
        stock.make_stock_item(ctx, item, "fifo")
        trusted = stock.Trusted("backup fixture")
        stock.receive(ctx, new_id(), item, warehouse, "3.125", "12.3456", date(2026, 10, 1), trusted=trusted)
        trusted.verify(ctx)
    upgradeable_books(engine, sales=br.chains()[1]["sales"][0], stock=stock_revision)
    before = snapshot(engine)
    assert before["stock_ledger_entry"] and before["stock_layer"]
    br.backup(engine, tmp_path / "b")
    target = pg_new()
    ru.restore(tmp_path / "b", target)
    restored = open_target(target)
    try:
        after = snapshot(restored)
        for name, rows in before.items():
            if name not in ("plugin_install", "alembic_version"):
                assert after[name] == rows, name
        with restored.connect() as conn:
            assert stock.verify_stock(company_ctx(conn, company_name="Stark Manufacturing")) == []
    finally:
        restored.dispose()


def test_copied_code_and_session_fail_on_the_first_host_calls(engine, runtime_login, tmp_path, pg_new):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from erpclaw_api.app import create_app
    from erpclaw_api.pipe import ListenerApp
    from erpclaw_assistant.model import ScriptedModel
    from erpclaw_core import administration

    with engine.begin() as conn:
        code = administration.mint_sign_in_code(conn, "owner", "owner")
    source_runtime = create_engine(runtime_login)
    try:
        client = TestClient(ListenerApp(create_app(source_runtime, ScriptedModel([])), "host", port=8740),
                            base_url="http://127.0.0.1:8740")
        response = client.post("/v1/session", json={"sign_in_code": code})
        assert response.status_code == 200, response.text
        secret = response.json()["session_secret"]
    finally:
        source_runtime.dispose()
    with engine.begin() as conn:
        unused = administration.mint_sign_in_code(conn, "owner", "owner")
    br.backup(engine, tmp_path / "b")
    target = pg_new(make_url(runtime_login).username)
    ru.restore(tmp_path / "b", target)
    runtime = create_engine(make_url(runtime_login).set(database=target.name))
    restored = open_target(target)
    try:
        client = TestClient(ListenerApp(create_app(runtime, ScriptedModel([])), "host", port=8740),
                            base_url="http://127.0.0.1:8740")
        response = client.post("/v1/session", json={"sign_in_code": unused})
        assert response.status_code == 401 and response.json()["code"] == "sign_in_code_invalid"
        response = client.get("/v1/me", headers={"Authorization": f"Session {secret}"})
        assert response.status_code == 401 and response.json()["code"] == "unauthenticated"
        with restored.begin() as conn:
            new = administration.mint_sign_in_code(conn, "owner", "owner")
        assert client.post("/v1/session", json={"sign_in_code": new}).status_code == 200
    finally:
        runtime.dispose()
        restored.dispose()


def test_unicode_line_separators_are_content_not_records(tmp_path):
    """Canonical JSON leaves Unicode separators inside strings; only LF separates table records."""
    lines = {"alembic_version": [json.dumps({"version_num": core_head()})],
             "company": [json.dumps({"name": "Café\u2028Second\u0085Third"}, ensure_ascii=False)]}
    (tmp_path / "tables").mkdir()
    manifest = {"format": bk.FORMAT, "tables": {}}
    for name, rows in lines.items():
        (tmp_path / "tables" / f"{name}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        manifest["tables"][name] = {"rows": len(rows), "sha256": _sha(rows)}
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert ru.read(tmp_path) == (manifest, lines)
