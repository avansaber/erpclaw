"""The service's exporter reads the books' own schema from the catalog, writes the engine's backup format and
canonical encoding, and records the books' schema version apart from the program that wrote it. On the same books it
is byte-identical to the engine's own export (`backup.backup`), file by file; on books at an older revision, where
that export (the running code's tables) does not match the schema, it succeeds."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.exc import DBAPIError

from backup_support import open_target, older_books, pg_new  # noqa: F401  (a fixture)
from erpclaw_core import backup as bk, backup_reflect as br, files, ledger, models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_core.db import metadata
from erpclaw_core.migrate import core_head
from erpclaw_core.setup import create_company

PINNED = datetime(2026, 11, 2, 9, 14, 0, 123456, tzinfo=timezone.utc)
ADDED = {"schema_version", "format_version", "written_by"}  # the additions to the engine's manifest, nothing else
PDF = b"%PDF-1.7\n" + b"a document the backup must carry\n" * 20


class _Pinned(datetime):
    @classmethod
    def now(cls, tz=None):
        return PINNED if tz is not None else PINNED.replace(tzinfo=None)


@pytest.fixture
def pinned_clock(monkeypatch):
    """Both exporters write the same `created_at`, so the manifests compare byte for byte."""
    monkeypatch.setattr(bk, "datetime", _Pinned)
    monkeypatch.setattr(br, "datetime", _Pinned)


def _post(engine, company, amount, d=date(2026, 10, 1), credit="4100"):
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name=company)
        a = lambda n: ledger.account_by_number(ctx, n)["id"]  # noqa: E731
        return ledger.post(ctx, d, "journal", new_id(), [ledger.Line(a("1100"), debit=Decimal(amount)),
                                                         ledger.Line(a(credit), credit=Decimal(amount))])


@pytest.fixture
def books(engine):
    """The demo books (every plugin installed), companies at each money scale, a reversal, a message in the outbox
    (a table whose column order in the books differs from the code's) and a stored file."""
    for name, cur, amount in (("Second Co", "USD", "12.34"), ("Yen Co", "JPY", "1500"), ("Dinar Co", "KWD", "1.250")):
        create_company(engine, name, currency=cur)
        r = _post(engine, name, amount)
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="Dinar Co")
        ledger.reverse(ctx, r.posting_id, date(2026, 10, 2))
        files.put(company_ctx(conn, company_name="Second Co"), PDF, "application/pdf", ("sales_invoice", "inv-1"))
        conn.execute(m.outbox.insert().values(
            id=new_id(), company_id=ctx.company_id, slot="send_message", recipient="ap@acme.test", subject="Invoice",
            body="Please find it attached.", status="queued",
            created_at=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc),
            next_try_at=datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc), tries=2))
    return engine


def _tree(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def _engine_bytes(manifest: dict) -> bytes:
    """A manifest as `backup.backup` writes it."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


# --- the format's column order, from the catalog -------------------------------------------------------------------

def test_the_catalog_gives_every_table_its_format_column_order(books):
    """`backup.backup` writes each row's columns in the code's model order. The exporter takes them from the catalog
    (ordinal order, and the format's pinned exceptions where a migration added a column in another place); for every
    table of books at this revision the two agree, so a later migration that adds a column elsewhere fails here
    first."""
    with books.connect() as conn:
        reflected = br.reflect(conn)
        assert [c.name for c in reflected.tables["supplier_payable"].columns] == [
            "id", "company_id", "supplier_id", "source_type", "source_id", "source_number",
            "posting_date", "currency", "amount", "outstanding", "status", "created_at",
            "posting_id", "control_line_no", "source_voucher_id", "opening_sequence", "old_reference", "due_date",
        ]
        for name, table in reflected.tables.items():
            if name == bk.VERSION_TABLE:
                continue
            assert br.key_order(table) == [c.name for c in metadata.tables[name].columns], name


# --- byte identity -------------------------------------------------------------------------------------------------

def test_the_reflecting_export_is_byte_identical_to_the_engines_file_by_file(books, tmp_path, pinned_clock):
    theirs = bk.backup(books, tmp_path / "engine")
    mine = br.backup(books, tmp_path / "reflect")
    a, b = _tree(tmp_path / "engine"), _tree(tmp_path / "reflect")
    assert sorted(a) == sorted(b)
    assert any(name.startswith("files/") for name in a) and theirs["files"] == 1
    assert a["tables/outbox.jsonl"].count(b"\n") >= 1  # a row whose column order the catalog does not give alone
    for name in a:
        if name != "MANIFEST.json":
            assert a[name] == b[name], name
    # The manifest: the engine's own keys byte for byte (its bytes are what its serialisation of them gives), and
    # the additions beside them, nothing else.
    assert set(mine) - set(theirs) == ADDED and set(theirs) <= set(mine)
    assert _engine_bytes(theirs) == a["MANIFEST.json"]
    ours = {k: v for k, v in json.loads(b["MANIFEST.json"]).items() if k not in ADDED}
    assert _engine_bytes(ours) == a["MANIFEST.json"]


def test_the_manifest_records_the_books_schema_apart_from_the_program_that_wrote_it(books, tmp_path):
    manifest = br.backup(books, tmp_path / "b", schema_release="3.0.2")
    with books.connect() as conn:
        rows = set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    sv = manifest["schema_version"]
    assert sv["release"] == "3.0.2" and sv["core_revision"] == core_head() == manifest["core_revision"]
    assert set(sv["plugins"].values()) | {sv["core_revision"]} == rows
    assert {"sales", "stock"} <= set(sv["plugins"])
    assert manifest["format_version"] == manifest["format"] == bk.FORMAT
    assert manifest["written_by"] == {"program": "erpclaw-core", "version": version("erpclaw-core")}
    on_disk = json.loads((tmp_path / "b" / "MANIFEST.json").read_text())
    assert on_disk == manifest


def test_the_reflecting_export_restores_with_the_engines_own_restore(books, tmp_path, pg_new):
    """The same format: the engine's unchanged restore takes it (it ignores the additions)."""
    br.backup(books, tmp_path / "b")
    target = pg_new()
    bk.restore(tmp_path / "b", target)
    restored = open_target(target)
    try:
        assert bk.book_diff(books, restored) == []
    finally:
        restored.dispose()


# --- older books ---------------------------------------------------------------------------------------------------

def test_older_books_export_where_the_engines_export_does_not_match_their_schema(engine, tmp_path):
    older_books(engine)
    with pytest.raises((bk.BackupRefused, DBAPIError, KeyError)):
        bk.backup(engine, tmp_path / "engine")  # the running code's tables are not these books' tables
    manifest = br.backup(engine, tmp_path / "b", schema_release="3.0.0")
    assert manifest["core_revision"] == "0004"
    assert manifest["schema_version"] == {"release": "3.0.0", "core_revision": "0004",
                                          "plugins": {"sales": "sales_0001"}}
    present = MetaData()
    present.reflect(engine)
    assert set(manifest["tables"]) == set(present.tables) | {bk.VERSION_TABLE}
    assert "books_identity" not in manifest["tables"] and manifest["books_id"] is None
    assert manifest["tables"]["sales_invoice"]["owner"] == "sales" and manifest["tables"]["company"]["owner"] == "core"
    company = [json.loads(line) for line in (tmp_path / "b" / "tables" / "company.jsonl").read_text().splitlines()]
    assert [c["created_at"] for c in company] == ["2026-10-26T12:00:00.000000Z"] * 2
    assert "date_order" not in company[0]  # the books' own columns at 0004, not the code's
    assert manifest["plugins"][0]["version"] == "0.0.0"  # as the books record it
    assert manifest["files"] == 0


# --- refusals ------------------------------------------------------------------------------------------------------

def test_a_table_nobody_owns_refuses_the_backup(books, tmp_path):
    with books.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE stray (id integer PRIMARY KEY)")
    with pytest.raises(bk.BackupRefused, match="stray"):
        br.backup(books, tmp_path / "b")
    assert not (tmp_path / "b").exists()


def test_books_at_a_revision_this_program_does_not_know_refuse_the_backup(books, tmp_path):
    with books.begin() as conn:
        conn.exec_driver_sql("UPDATE alembic_version SET version_num = '0099' WHERE version_num = %(h)s",
                             {"h": core_head()})
    with pytest.raises(bk.BackupRefused, match="0099"):
        br.backup(books, tmp_path / "b")


def test_a_backup_never_overwrites(books, tmp_path):
    (tmp_path / "b").mkdir()
    with pytest.raises(bk.BackupRefused, match="already exists") as refusal:
        br.backup(books, tmp_path / "b")
    message = str(refusal.value)
    assert message == "The backup destination already exists; a backup never overwrites."
    assert "/" not in message and "\\" not in message


def test_a_planted_file_mismatch_fails_the_backup(books, tmp_path):
    with books.connect() as conn:
        row = conn.execute(m.file_object.select()).mappings().one()
        payload = files.file_path(files.root_dir(conn), row["company_id"], row["sha256"])
    payload.write_bytes(b"altered")
    with pytest.raises(bk.BackupRefused, match="backup failed"):
        br.backup(books, tmp_path / "b")
    assert not (tmp_path / "b" / "MANIFEST.json").exists()


def test_the_export_uses_one_read_only_repeatable_snapshot(books, tmp_path, monkeypatch):
    original = br.dump
    changed = []

    def dump(conn, table, order=None):
        assert conn.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on"
        assert conn.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read"
        if not changed:
            # Reflection already established the snapshot. A committed concurrent edit must not leak in.
            with books.begin() as writer:
                writer.execute(m.company.update().where(m.company.c.name == "Second Co").values(name="Later Co"))
            changed.append(True)
        return original(conn, table, order)
    monkeypatch.setattr(br, "dump", dump)
    br.backup(books, tmp_path / "b")
    rows = [json.loads(line) for line in (tmp_path / "b" / "tables" / "company.jsonl").read_text().splitlines()]
    assert "Second Co" in {row["name"] for row in rows}
    assert "Later Co" not in {row["name"] for row in rows}


def test_fifo_layers_keep_self_named_roots_and_load_parents_first():
    # Roots point to themselves by design. Another layer in the group must follow its root even by reverse PK.
    from erpclaw_stock.tables import layer

    rows = [{"id": "a", "group_id": "z"}, {"id": "z", "group_id": "z"}]
    assert br.restore_order(layer, rows) == [rows[1], rows[0]]
    assert rows == [{"id": "a", "group_id": "z"}, {"id": "z", "group_id": "z"}]
    with pytest.raises(bk.BackupRefused, match="cycle"):
        br.restore_order(layer, [{"id": "a", "group_id": "b"}, {"id": "b", "group_id": "a"}])
