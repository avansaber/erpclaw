"""Backup and restore of the stored files: a backup copies and checks every file a row names; a restore checks them
before anything is published, stages them under its own locked marker, publishes them as the new database's root
before the database, and never writes the live books' root. Cleanup removes only what this server's own restores
marked and no longer hold; a root without a marker is never removed."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from erpclaw_core import backup as bk, files, models as m, retention
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.setup import create_company

from test_backup import _databases, _nothing_left, _open, _target, restored_databases  # noqa: F401

PDF = b"%PDF-1.7\n" + b"an invoice to back up\n" * 25


class Died(BaseException):
    """The restoring process ends here: no cleanup of its own runs."""


@pytest.fixture
def stored(engine):
    """Books with stored files for two companies, one file shared by two documents."""
    create_company(engine, "Second Co")
    ids = {}
    with engine.begin() as conn:
        for company, doc, data in ((None, "inv-1", PDF), (None, "inv-2", PDF), ("Second Co", "s-1", PDF + b"s\n")):
            ctx = company_ctx(conn, company_name=company) if company else company_ctx(conn, company_name=_first(conn))
            ids[doc] = files.put(ctx, data, "application/pdf", ("sales_invoice", doc))
    return engine, ids


def _first(conn) -> str:
    return conn.execute(select(m.company.c.name).order_by(m.company.c.created_at)).scalars().first()


def _snapshot_root(engine) -> dict[str, bytes]:
    with engine.connect() as conn:
        root = files.root_dir(conn)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _get(engine, fid) -> bytes:
    with engine.connect() as conn:
        r = conn.execute(select(m.file_object).where(m.file_object.c.id == fid)).mappings().one()
        return files.get(company_ctx(conn, company_id=r["company_id"]), fid)


def _root_of(name: str) -> Path:
    return files.files_dir() / files.root_name(name)


def _stagings() -> list[Path]:
    base = files.files_dir()
    return sorted(base.glob(f"{files.RESTORE_PREFIX}*")) if base.is_dir() else []


def _backup_file(path: Path, engine, fid) -> Path:
    with engine.connect() as conn:
        r = conn.execute(select(m.file_object).where(m.file_object.c.id == fid)).mappings().one()
    return files.file_path(path / "files", r["company_id"], r["sha256"])


# --- backup ----------------------------------------------------------------------------------------------------------

def test_a_backup_copies_every_referenced_file_once_and_states_how_many(stored, tmp_path):
    engine, ids = stored
    manifest = bk.backup(engine, tmp_path / "b")
    assert manifest["files"] == 2  # three rows, two distinct files
    for fid in ids.values():
        assert _backup_file(tmp_path / "b", engine, fid).read_bytes() == _get(engine, fid)


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_a_backup_whose_file_differs_from_its_row_fails(stored, tmp_path, damage):
    engine, ids = stored
    with engine.connect() as conn:
        r = conn.execute(select(m.file_object).where(m.file_object.c.id == ids["s-1"])).mappings().one()
        path = files.file_path(files.root_dir(conn), r["company_id"], r["sha256"])
    if damage == "changed":
        path.write_bytes(path.read_bytes()[:-1] + b"X")
    else:
        path.unlink()
    with pytest.raises(bk.BackupRefused, match="the backup failed"):
        bk.backup(engine, tmp_path / "b")


# --- restore ---------------------------------------------------------------------------------------------------------

def test_a_restore_publishes_the_new_root_with_its_marker_and_leaves_the_live_files_identical(stored, tmp_path):
    engine, ids = stored
    bk.backup(engine, tmp_path / "b")
    live_before = _snapshot_root(engine)
    target = _target()
    bk.restore(tmp_path / "b", target)
    restored = _open(target)
    try:
        root = _root_of(target.name)
        marker = json.loads((root / files.ROOT_MARKER).read_text())
        with engine.connect() as conn:
            assert marker == {"books_id": files.books_id(conn), "database": target.name, "format": 1}
        assert not (root / files.RESTORE_MARKER).exists() and _stagings() == []
        for fid in ids.values():
            assert _get(restored, fid) == _get(engine, fid)
        files.check_root(restored)  # the restored books start
        assert bk.verify(tmp_path / "b", target) == []
    finally:
        restored.dispose()
    assert _snapshot_root(engine) == live_before


def test_restored_books_read_files_only_from_their_own_root(stored, tmp_path):
    engine, ids = stored
    bk.backup(engine, tmp_path / "b")
    target = _target()
    bk.restore(tmp_path / "b", target)
    restored = _open(target)
    try:
        with engine.connect() as conn:
            r = conn.execute(select(m.file_object).where(m.file_object.c.id == ids["inv-1"])).mappings().one()
            live = files.file_path(files.root_dir(conn), r["company_id"], r["sha256"])
        live.write_bytes(b"damaged in the live books")
        assert _get(restored, ids["inv-1"]) == PDF  # the restored root is untouched by the live one
        with pytest.raises(Refusal):
            _get(engine, ids["inv-1"])
    finally:
        restored.dispose()


@pytest.mark.parametrize("damage", ["missing", "one_byte", "wrong_size"])
def test_a_restore_with_a_bad_file_refuses_before_anything_is_published(stored, tmp_path, damage):
    engine, ids = stored
    bk.backup(engine, tmp_path / "b")
    path = _backup_file(tmp_path / "b", engine, ids["s-1"])
    data = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "one_byte":
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    else:
        path.write_bytes(data + b"\n")
    target = _target()
    with pytest.raises(bk.BackupRefused, match="files do not match its rows"):
        bk.restore(tmp_path / "b", target)
    _nothing_left(target)
    assert not _root_of(target.name).exists() and _stagings() == []


def test_a_restore_never_writes_over_a_root_that_exists(stored, tmp_path):
    engine, _ = stored
    bk.backup(engine, tmp_path / "b")
    target = _target()
    existing = _root_of(target.name)
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("someone's files")
    with pytest.raises(bk.BackupRefused, match="already exists"):
        bk.restore(tmp_path / "b", target)
    _nothing_left(target)
    assert (existing / "keep.txt").read_text() == "someone's files" and _stagings() == []


def _die_at(monkeypatch, step):
    def hook(at, **kw):
        if at == step:
            raise Died(at)

    monkeypatch.setattr(bk, "_HOOK", hook)
    monkeypatch.setattr(files.RestoreFiles, "abandon", lambda self: self.release())  # a dead process cleans nothing


def _age(folder: Path, hours: float = 2) -> None:
    old = time.time() - hours * 3600
    os.utime(folder / files.RESTORE_MARKER, (old, old))


def _cleanup() -> dict:
    from conftest import PG_URL

    return bk.clean_restored_files(PG_URL)


def test_a_crash_before_the_files_rename_leaves_a_marked_staging_folder_the_cleanup_removes(stored, tmp_path,
                                                                                           monkeypatch):
    engine, _ = stored
    bk.backup(engine, tmp_path / "b")
    live_before = _snapshot_root(engine)
    target = _target()
    _die_at(monkeypatch, "files_staged")
    with pytest.raises(Died):
        bk.restore(tmp_path / "b", target)
    assert _databases(target.name) == [] and not _root_of(target.name).exists()
    [staging] = _stagings()
    assert _cleanup()["removed"] == []  # under an hour old: a restore may still be running
    _age(staging)
    assert _cleanup()["removed"] == [staging.name] and _stagings() == []
    assert _snapshot_root(engine) == live_before


def test_a_crash_between_the_files_rename_and_the_database_leaves_a_marked_root_the_cleanup_removes(
        stored, tmp_path, monkeypatch):
    engine, _ = stored
    bk.backup(engine, tmp_path / "b")
    live_before = _snapshot_root(engine)
    target = _target()
    _die_at(monkeypatch, "files_published")
    with pytest.raises(Died):
        bk.restore(tmp_path / "b", target)
    root = _root_of(target.name)
    assert _databases(target.name) == [] and (root / files.RESTORE_MARKER).exists()
    _age(root)
    assert _cleanup()["removed"] == [root.name] and not root.exists()
    assert _snapshot_root(engine) == live_before


def test_a_crash_after_the_database_is_published_leaves_complete_books_whose_files_verify(stored, tmp_path,
                                                                                         monkeypatch):
    engine, ids = stored
    bk.backup(engine, tmp_path / "b")
    target = _target()
    _die_at(monkeypatch, "published")
    with pytest.raises(Died):
        bk.restore(tmp_path / "b", target)
    monkeypatch.setattr(bk, "_HOOK", None)
    assert bk.verify(tmp_path / "b", target) == []
    root = _root_of(target.name)
    _age(root)
    out = _cleanup()
    assert out["unmarked"] == [root.name] and out["removed"] == []
    assert root.is_dir() and not (root / files.RESTORE_MARKER).exists()
    restored = _open(target)
    try:
        files.check_root(restored)
        assert _get(restored, ids["s-1"]) == PDF + b"s\n"
    finally:
        restored.dispose()


# --- what cleanup never removes --------------------------------------------------------------------------------------

def _marked_root(name: str, server: str, hours_old: float = 2) -> Path:
    root = _root_of(name)
    root.mkdir(parents=True)
    (root / files.RESTORE_MARKER).write_text(json.dumps({"database": name, "format": 1, "restore_id": "r",
                                                         "system_identifier": server}))
    _age(root, hours_old)
    return root


def _this_server() -> str:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from conftest import PG_URL

    e = create_engine(make_url(PG_URL).set(database="postgres"), poolclass=NullPool)
    try:
        with e.connect() as c:
            return files.server_identity(c)
    finally:
        e.dispose()


def test_a_plain_root_whose_database_is_not_on_this_server_survives_retire_and_is_reported(engine):
    root = _root_of("books_on_another_server")
    root.mkdir(parents=True)
    (root / files.ROOT_MARKER).write_bytes(files._marker("00000000-0000-4000-8000-000000000000",
                                                         "books_on_another_server"))
    out = retention.retire(engine)
    assert root.is_dir() and (root / files.ROOT_MARKER).exists()
    assert out["restore_marks"]["roots_without_database"] == [root.name]


def test_a_marked_root_naming_another_servers_identity_survives(engine):
    root = _marked_root("restored_elsewhere", "1234567890123456789")
    out = retention.retire(engine)
    assert root.is_dir() and out["restore_marks"]["removed"] == []


def test_a_marked_root_under_an_hour_old_survives(engine):
    root = _marked_root("restoring_now", _this_server(), hours_old=0.1)
    retention.retire(engine)
    assert root.is_dir()


def test_a_marked_root_whose_lock_is_held_survives_even_when_older_than_an_hour(engine):
    root = _marked_root("restoring_slowly", _this_server(), hours_old=5)
    fd = files._lock_marker(root / files.RESTORE_MARKER, blocking=True)  # a long restore holds it
    try:
        out = retention.retire(engine)
        assert root.is_dir() and out["restore_marks"]["removed"] == []
    finally:
        os.close(fd)
    out = retention.retire(engine)  # its restore has ended without finishing: now it goes
    assert not root.exists() and out["restore_marks"]["removed"] == [root.name]


def test_the_restore_marker_of_a_completed_restore_is_gone(stored, tmp_path):
    engine, _ = stored
    bk.backup(engine, tmp_path / "b")
    target = _target()
    bk.restore(tmp_path / "b", target)
    root = _root_of(target.name)
    assert root.is_dir() and not (root / files.RESTORE_MARKER).exists()


@pytest.mark.parametrize("stage", ["before_publish", "publishing", "published"])
def test_a_failure_at_each_stage_of_publication_keeps_the_restored_files(stored, tmp_path, monkeypatch, stage):
    """Publication can fail before its rename, inside its transaction, or after its commit (a lost connection in its
    own check): whichever, the restored root stays whole with its marker, and the cleanup then clears the marker if
    the database exists, or removes the root if it does not. The real cleanup runs (nothing is patched away)."""
    from sqlalchemy.exc import OperationalError

    engine, ids = stored
    bk.backup(engine, tmp_path / "b")
    live_before = _snapshot_root(engine)
    target = _target()

    def lost(at, **kw):
        if at == stage:
            raise OperationalError("publication", {}, Exception("server closed the connection"))

    monkeypatch.setattr(bk, "_HOOK", lost)
    with pytest.raises(OperationalError):
        bk.restore(tmp_path / "b", target)
    monkeypatch.setattr(bk, "_HOOK", None)
    root = _root_of(target.name)
    assert root.is_dir() and (root / files.RESTORE_MARKER).exists()
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(select(m.file_object)).mappings()]
    assert files.check_restored(root, rows) == []  # every file whole
    published = _databases(target.name) == [target.name]
    assert published == (stage == "published")
    assert _databases("erpclaw\\_restore\\_%") == []  # no staging database is left behind
    if published:
        assert bk.verify(tmp_path / "b", target) == []
    _age(root)
    out = _cleanup()
    if published:
        assert out["unmarked"] == [root.name] and root.is_dir()
    else:
        assert out["removed"] == [root.name] and not root.exists()
    assert _snapshot_root(engine) == live_before


def test_a_backup_file_that_is_a_link_inside_the_backup_is_refused(stored, tmp_path):
    """A link that stays inside the backup still is not a plain file: refused."""
    engine, ids = stored
    bk.backup(engine, tmp_path / "b")
    path = _backup_file(tmp_path / "b", engine, ids["s-1"])
    twin = path.with_name("copy-of-the-bytes")
    path.rename(twin)
    path.symlink_to(twin)
    target = _target()
    with pytest.raises(bk.BackupRefused, match="not a plain file inside"):
        bk.restore(tmp_path / "b", target)
    _nothing_left(target)


@pytest.mark.parametrize("link", ["file", "folder"])
def test_a_backup_whose_file_or_folder_is_a_link_outside_it_is_refused(stored, tmp_path, link):
    """The bytes are right (a copy of the real file), but the backup reaches outside itself: refused before
    anything is staged."""
    engine, ids = stored
    bk.backup(engine, tmp_path / "b")
    path = _backup_file(tmp_path / "b", engine, ids["s-1"])
    outside = tmp_path / "outside" / path.parent.parent.name / path.parent.name
    outside.mkdir(parents=True)
    (outside / path.name).write_bytes(path.read_bytes())
    if link == "file":
        path.unlink()
        path.symlink_to(outside / path.name)
    else:
        company = path.parent.parent
        import shutil

        shutil.rmtree(company)
        company.symlink_to(outside.parent)
    target = _target()
    with pytest.raises(bk.BackupRefused, match="leaves its root|not a plain file inside"):
        bk.restore(tmp_path / "b", target)
    _nothing_left(target)
    assert not _root_of(target.name).exists() and _stagings() == []


def test_a_backup_naming_a_company_folder_outside_its_root_is_refused_and_the_live_root_is_untouched(stored,
                                                                                                tmp_path):
    from test_backup import _rewrite

    engine, ids = stored
    bk.backup(engine, tmp_path / "b")
    live_before = _snapshot_root(engine)
    with engine.connect() as conn:
        live = files.root_dir(conn).name

    def escape(lines):
        out = []
        for line in lines:
            row = json.loads(line)
            row["company_id"] = f"../{live}/x"
            out.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
        return out

    _rewrite(tmp_path / "b", "file_object", escape)
    target = _target()
    with pytest.raises(bk.BackupRefused):
        bk.restore(tmp_path / "b", target)
    _nothing_left(target)
    assert _snapshot_root(engine) == live_before and _stagings() == []


def test_a_backup_whose_files_folder_is_a_link_outside_it_is_refused(stored, tmp_path):
    """The files folder itself points outside the backup, at a faithful copy: refused, nothing staged."""
    import shutil

    engine, _ = stored
    bk.backup(engine, tmp_path / "b")
    outside = tmp_path / "elsewhere"
    shutil.copytree(tmp_path / "b" / "files", outside)
    shutil.rmtree(tmp_path / "b" / "files")
    (tmp_path / "b" / "files").symlink_to(outside)
    target = _target()
    with pytest.raises(bk.BackupRefused, match="not a plain file inside"):
        bk.restore(tmp_path / "b", target)
    _nothing_left(target)
    assert not _root_of(target.name).exists() and _stagings() == []


def _aged_mark(name: str) -> Path:
    return _marked_root(name, _this_server(), hours_old=2)


def test_cleanup_keeps_a_root_whose_database_is_published_after_its_first_look(monkeypatch):
    """The database appears between the cleanup's first look and its decision, as when a restore publishes and
    finishes meanwhile: the server is asked afresh under the lock, so the root stays and only loses its marker."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from conftest import PG_URL

    name = _target().name  # a database name the test owns and drops
    root = _aged_mark(name)
    admin = create_engine(make_url(PG_URL).set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)

    def publish_meanwhile(at):
        if at == "cleanup_after_lock":
            with admin.connect() as c:
                c.exec_driver_sql(f'CREATE DATABASE "{name}"')

    monkeypatch.setattr(files, "fault", publish_meanwhile)
    try:
        out = _cleanup()
    finally:
        admin.dispose()
    assert root.is_dir() and out["removed"] == [] and out["unmarked"] == [root.name]


@pytest.mark.parametrize("change", ["finished", "replaced"])
def test_cleanup_skips_a_mark_that_changed_after_its_first_look(monkeypatch, change):
    """A restore finishing (its marker removed) or a new restore's marker in its place, between the first look and
    the lock: the cleanup acts only on the marker it read, so it leaves the root alone."""
    name = _target().name
    root = _aged_mark(name)
    marker = root / files.RESTORE_MARKER

    def meanwhile(at):
        if at == "cleanup_after_lock":
            text = marker.read_text()
            marker.unlink()
            if change == "replaced":
                marker.write_text(text.replace('"r"', '"r2"'))

    monkeypatch.setattr(files, "fault", meanwhile)
    out = _cleanup()
    assert root.is_dir() and out["removed"] == [] and out["unmarked"] == []


def test_a_restore_whose_engine_home_has_no_parent_is_refused_naming_the_paths(stored, tmp_path, monkeypatch):
    """Restore is the operator's command: its refusal names the paths, and nothing is made or published."""
    engine, _ = stored
    bk.backup(engine, tmp_path / "b")
    home = tmp_path / "x" / "y" / "home"
    monkeypatch.setenv("ERPCLAW_HOME", str(home))
    target = _target()
    with pytest.raises(bk.BackupRefused, match="cannot be made: its folder"):
        bk.restore(tmp_path / "b", target)
    _nothing_left(target)
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("nth", [1, 2], ids=["home", "files folder"])
def test_a_restore_retry_after_an_interrupted_folder_syncs_it_and_the_homes_parent_before_publishing(stored, tmp_path,
                                                                                                  monkeypatch, nth):
    """A restore into a new engine home crashes right after making the home (1) or its files folder (2); the retry
    finds the folder there and must sync the files folder, the home and the home's parent before it publishes."""
    engine, _ = stored
    bk.backup(engine, tmp_path / "b")
    home = tmp_path / "new-home"
    monkeypatch.setenv("ERPCLAW_HOME", str(home))
    made = []

    def crash(at):
        if at == "after_mkdir":
            made.append(at)
            if len(made) == nth:
                raise Died(at)

    monkeypatch.setattr(files, "fault", crash)
    with pytest.raises(Died):
        bk.restore(tmp_path / "b", _target())
    assert home.is_dir() and _databases("erpclaw\\_restore\\_%") == []
    monkeypatch.setattr(files, "fault", lambda at: None)
    events = []
    real = files._sync_dir
    monkeypatch.setattr(files, "_sync_dir", lambda p: (events.append(Path(os.path.abspath(p))), real(p))[1])
    monkeypatch.setattr(bk, "_HOOK", lambda at, **kw: events.append(at) if at == "before_publish" else None)
    target = _target()
    bk.restore(tmp_path / "b", target)
    before = events[:events.index("before_publish")]
    for folder in (home / "files", home, home.parent):
        assert folder in before
    assert bk.verify(tmp_path / "b", target) == []


def test_put_and_a_restores_staging_sync_through_the_one_routine(stored, tmp_path, monkeypatch):
    engine, _ = stored
    bk.backup(engine, tmp_path / "b")
    calls = []
    real = files._sync_path
    monkeypatch.setattr(files, "_sync_path", lambda p: (calls.append(Path(p)), real(p))[1])
    with engine.begin() as conn:
        files.put(company_ctx(conn, company_name=_first(conn)), PDF + b"one more\n", "application/pdf",
                  ("sales_invoice", "inv-3"))
    assert calls and calls[-1].parent.parent.parent.parent == files.files_dir()  # the put's file, through it
    calls.clear()
    bk.restore(tmp_path / "b", _target())
    assert files.files_dir() in calls  # the restore's staging, through the same routine
