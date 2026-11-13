"""The file store: a committed row always names a complete file that reached the disk; reads check the hash; no
company reads another's file; the books' files are found only under their own root, marked with their identity and
name; the database refuses every change to a stored file's row but its retirement after its date."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from erpclaw_core import files, models as m, retention
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.db import books_id, make_engine
from erpclaw_core.files import FilesRefused
from erpclaw_core.setup import create_company

PDF = b"%PDF-1.7\n" + b"stored invoice bytes\n" * 40
DOC = ("sales_invoice", "inv-0001")


class Crash(Exception):
    pass


def _put(engine, data=PDF, document=DOC, company=None, **kw) -> str:
    with engine.begin() as conn:
        return files.put(company_ctx(conn, company_name=company), data, "application/pdf", document, **kw)


def _get(engine, fid, company=None) -> bytes:
    with engine.connect() as conn:
        return files.get(company_ctx(conn, company_name=company), fid)


def _row(engine, fid) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(select(m.file_object).where(m.file_object.c.id == fid)).mappings().one())


def _path(engine, fid) -> Path:
    r = _row(engine, fid)
    with engine.connect() as conn:
        return files.file_path(files.root_dir(conn), r["company_id"], r["sha256"])


def _rows(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM file_object")).scalar()


def _second_company(engine) -> str:
    create_company(engine, "Beta Traders")
    return "Beta Traders"


# --- put and get -----------------------------------------------------------------------------------------------------

def test_put_then_get_returns_the_bytes_and_the_row_names_the_document(engine):
    fid = _put(engine)
    assert _get(engine, fid) == PDF
    r = _row(engine, fid)
    assert (r["sha256"], r["size"], r["media_type"]) == (hashlib.sha256(PDF).hexdigest(), len(PDF), "application/pdf")
    assert (r["voucher_type"], r["voucher_id"], r["retention_until"], r["render_inputs"]) == (*DOC, None, None)


def test_a_byte_changed_on_disk_is_refused_at_get(engine):
    fid = _put(engine)
    path = _path(engine, fid)
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(bytes(data))
    with pytest.raises(Refusal) as e:
        _get(engine, fid)
    assert e.value.code == "file_altered"


def test_a_missing_file_is_refused_at_get(engine):
    fid = _put(engine)
    _path(engine, fid).unlink()
    with pytest.raises(Refusal) as e:
        _get(engine, fid)
    assert e.value.code == "file_altered"


def test_another_companys_file_id_is_refused(engine):
    with engine.connect() as conn:
        mine = conn.execute(select(m.company.c.name)).scalar_one()
    other = _second_company(engine)
    fid = _put(engine, company=other)
    with pytest.raises(Refusal) as e:
        _get(engine, fid, company=mine)
    assert e.value.code == "file_not_found"
    assert _get(engine, fid, company=other) == PDF


@pytest.mark.parametrize("media_type", ["image/png", "text/html", "application/x-pdf"])
def test_a_type_other_than_pdf_is_refused(engine, media_type):
    with engine.begin() as conn, pytest.raises(Refusal) as e:
        files.put(company_ctx(conn), PDF, media_type, DOC)
    assert e.value.code == "file_type_refused"
    assert _rows(engine) == 0


@pytest.mark.parametrize("size", [0, files.MAX_SIZE + 1])
def test_an_empty_file_and_one_over_10_mb_are_refused(engine, size):
    with engine.begin() as conn, pytest.raises(Refusal) as e:
        files.put(company_ctx(conn), b"x" * size, "application/pdf", DOC)
    assert e.value.code == "file_size_refused"


def test_a_file_of_exactly_10_mb_is_stored(engine):
    fid = _put(engine, data=b"%" * files.MAX_SIZE)
    assert _row(engine, fid)["size"] == files.MAX_SIZE


def test_the_same_bytes_for_one_company_are_stored_once_and_a_repeat_returns_the_row(engine):
    a = _put(engine)
    assert _put(engine) == a  # the same document and bytes: the same row
    b = _put(engine, document=("sales_invoice", "inv-0002"))
    assert b != a and _rows(engine) == 2
    assert _path(engine, a) == _path(engine, b)
    stored = [p for p in _path(engine, a).parent.iterdir()]
    assert stored == [_path(engine, a)]  # one file, no leftover temporary name


def test_a_damaged_copy_is_written_again_by_the_next_put(engine):
    a = _put(engine)
    _path(engine, a).write_bytes(b"damaged")
    b = _put(engine, document=("sales_invoice", "inv-0002"))
    assert _get(engine, b) == PDF and _get(engine, a) == PDF


# --- render inputs ---------------------------------------------------------------------------------------------------

def test_render_inputs_are_stored_as_canonical_json_whatever_the_key_order(engine):
    a = _put(engine, render_inputs={"template": "invoice/1", "fonts": {"sans": "Noto Sans", "bold": "Noto Bold"}})
    b = _put(engine, document=("sales_invoice", "inv-0002"),
             render_inputs={"fonts": {"bold": "Noto Bold", "sans": "Noto Sans"}, "template": "invoice/1"})
    assert _row(engine, a)["render_inputs"] == _row(engine, b)["render_inputs"] == \
        '{"fonts":{"bold":"Noto Bold","sans":"Noto Sans"},"template":"invoice/1"}'


@pytest.mark.parametrize("bad", [{"pages": 3}, {"a": {"b": [1]}}, {"x": None}, {"flag": True}, {1: "one"}])
def test_render_inputs_other_than_text_or_mappings_are_refused_in_plain_words(engine, bad):
    with engine.begin() as conn, pytest.raises(Refusal) as e:
        files.put(company_ctx(conn), PDF, "application/pdf", DOC, render_inputs=bad)
    assert e.value.code == "render_inputs_shape"
    assert str(e.value) == "The file could not be saved. Please try making it again; nothing was changed."


def test_the_database_refuses_render_inputs_that_are_not_json(engine):
    fid = _put(engine)
    r = _row(engine, fid)
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(m.file_object.insert().values({**r, "id": "x" * 36, "voucher_id": "inv-9", "render_inputs": "{"}))


# --- durable before referenced ---------------------------------------------------------------------------------------

PUT_POINTS = ["after_write", "after_file_sync", "after_rename", "after_folder_sync", "after_path_sync",
              "before_insert"]


@pytest.mark.parametrize("point", PUT_POINTS)
def test_a_crash_at_each_step_of_a_put_leaves_no_row_and_the_next_put_stores_a_complete_file(engine, monkeypatch,
                                                                                            point):
    _put(engine, document=("sales_invoice", "warm"))  # the root and folders exist; first writes are tested below

    def crash(at):
        if at == point:
            raise Crash(at)

    monkeypatch.setattr(files, "fault", crash)
    data = PDF + b"another\n"
    with pytest.raises(Crash):
        _put(engine, data=data)
    assert _rows(engine) == 1
    monkeypatch.setattr(files, "fault", lambda at: None)
    fid = _put(engine, data=data)
    assert _get(engine, fid) == data


def test_the_file_is_synced_renamed_and_its_folder_synced_before_the_row_is_inserted(engine, monkeypatch):
    _put(engine, document=("sales_invoice", "warm"))
    events = []
    real_fsync, real_replace, real_sync_dir = files.os.fsync, files.os.replace, files._sync_dir
    def fsync(fd):  # a file's own sync; a folder's is recorded by _sync_dir below
        if stat.S_ISREG(os.fstat(fd).st_mode):
            events.append("fsync")
        return real_fsync(fd)

    monkeypatch.setattr(files.os, "fsync", fsync)
    monkeypatch.setattr(files.os, "replace", lambda a, b: (events.append("rename"), real_replace(a, b))[1])
    monkeypatch.setattr(files, "_sync_dir", lambda p: (events.append(f"sync {Path(p).name}"), real_sync_dir(p))[1])
    monkeypatch.setattr(files, "fault", lambda at: events.append(at) if at == "before_insert" else None)
    data = PDF + b"order\n"
    sha = hashlib.sha256(data).hexdigest()
    _put(engine, data=data)
    renamed = events.index("rename")  # the new file's (the marker exists, so its path is only synced, not written)
    assert events[renamed - 1] == "fsync" and events[renamed + 1] == f"sync {sha[:2]}"  # synced, renamed, folder synced
    after = events[renamed + 2:]
    assert after[-1] == "before_insert" and f"sync {sha[:2]}" in after[:-1]  # the whole path again, then the row


def test_the_first_file_of_new_books_makes_each_folder_durably_from_the_top(engine, monkeypatch):
    from erpclaw_core.db import data_root

    data_root().mkdir(parents=True, exist_ok=True)  # the engine's home exists; its files folder does not
    synced = []
    real = files._sync_dir
    monkeypatch.setattr(files, "_sync_dir", lambda p: (synced.append(Path(p)), real(p))[1])
    fid = _put(engine)
    r = _row(engine, fid)
    with engine.connect() as conn:
        root = files.root_dir(conn)
    base = root.parent
    company, prefix = root / r["company_id"], root / r["company_id"] / r["sha256"][:2]
    # each new folder synced, then its parent, from the top; the marker's folder synced after its rename; then the
    # marker's and the file's whole paths synced again before the row (a retry may reuse what a crash left)
    home = base.parent

    def chain(p):  # a folder and every folder above it up to the home, then the home's parent
        return [p, *[q for q in p.parents if q == home or home in q.parents], home.parent]

    expected = [base, home, root, base, root, *chain(root), company, root, prefix, company, prefix, *chain(prefix)]
    assert synced == expected


RETRY_POINTS = ["after_mkdir", "after_dir_sync", "after_write", "after_file_sync", "after_rename", "after_folder_sync",
                "after_path_sync"]


@pytest.mark.parametrize("point", RETRY_POINTS)
def test_a_retry_after_a_crash_makes_the_left_file_and_folders_durable_before_its_row(engine, monkeypatch, point):
    """A put that crashed after its rename left the file and its new folders unsynced; the retry finds the file
    whole and must still sync it and every folder above it before the row commits."""
    from erpclaw_core.db import data_root

    data_root().mkdir(parents=True, exist_ok=True)

    def crash(at):
        if at == point:
            raise Crash(at)

    monkeypatch.setattr(files, "fault", crash)
    with pytest.raises(Crash):
        _put(engine)
    assert _rows(engine) == 0
    events = []
    real_fsync, real_sync_dir = files.os.fsync, files._sync_dir

    def fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            events.append("fsync file")
        return real_fsync(fd)

    monkeypatch.setattr(files.os, "fsync", fsync)
    monkeypatch.setattr(files, "_sync_dir", lambda p: (events.append(f"sync {Path(p).name}"), real_sync_dir(p))[1])
    monkeypatch.setattr(files, "fault", lambda at: events.append(at) if at == "before_insert" else None)
    fid = _put(engine)
    r = _row(engine, fid)
    with engine.connect() as conn:
        root = files.root_dir(conn)
    upto = events.index("before_insert")
    for name in (r["sha256"][:2], r["company_id"], root.name, "files"):
        assert f"sync {name}" in events[:upto]
    assert "fsync file" in events[:upto] and _get(engine, fid) == PDF


def test_a_retry_after_a_crash_while_making_the_engine_home_syncs_it_and_its_parent_and_nothing_above(engine, monkeypatch,
                                                                                                    tmp_path):
    """The engine home is missing (its parent exists); the first put crashes right after making it; the retry finds
    the home there and syncs it and its parent before the row, and no folder above the home's parent."""
    home = tmp_path / "a" / "home"
    home.parent.mkdir()
    monkeypatch.setenv("ERPCLAW_HOME", str(home))

    def crash(at):
        if at == "after_mkdir":
            raise Crash(at)  # the first folder made is the home itself

    monkeypatch.setattr(files, "fault", crash)
    with pytest.raises(Crash):
        _put(engine)
    assert home.is_dir() and _rows(engine) == 0
    synced = []
    real = files._sync_dir
    monkeypatch.setattr(files, "_sync_dir", lambda p: (synced.append(Path(os.path.abspath(p))), real(p))[1])
    monkeypatch.setattr(files, "fault", lambda at: synced.append(at) if at == "before_insert" else None)
    _put(engine)
    upto = synced.index("before_insert")
    folders = [p for p in synced[:upto] if isinstance(p, Path)]
    assert home in folders and home.parent in folders
    assert all(p == home.parent or home in [p, *p.parents] for p in folders)  # nothing above the home's parent


def test_a_put_never_makes_a_folder_above_the_engine_home(engine, monkeypatch, tmp_path):
    home = tmp_path / "a" / "b" / "home"
    monkeypatch.setenv("ERPCLAW_HOME", str(home))
    with engine.begin() as conn, pytest.raises(Refusal) as e:
        files.put(company_ctx(conn), PDF, "application/pdf", DOC)
    assert e.value.code == "files_home_missing" and not (tmp_path / "a").exists()
    assert str(tmp_path) not in e.value.message  # the owner may read it in chat: business words, no paths


def test_no_file_store_or_backup_code_makes_folders_with_parents():
    """The invariant (never a folder above the engine home) holds only while every folder is made by _make_dirs from
    the nearest existing one down; a makedirs or mkdir(parents=True) would quietly reopen it. The one exception is
    a backup's own destination, which is the operator's chosen path, never under the home."""
    import inspect as pyinspect
    import re

    from erpclaw_core import backup as bk, retention

    for mod in (files, retention):
        src = pyinspect.getsource(mod)
        assert not re.search(r"makedirs|parents\s*=\s*True", src), mod.__name__
    src = pyinspect.getsource(bk)
    assert re.findall(r"makedirs|parents\s*=\s*True", src) == ["parents=True"]
    assert '(path / "tables").mkdir(parents=True)' in src  # the backup's own destination folder


def test_a_company_id_or_sha256_that_could_leave_the_root_is_refused():
    root = Path("/tmp/erpclaw-root-never-written")
    sha = "a" * 64
    for company in ("../db-626f6f6b73", "..", "a/b", "/etc", "/tmp/x", "C0FFEE00-0000-4000-8000-000000000001", ""):
        with pytest.raises(files.FileCheckFailed):
            files.file_path(root, company, sha)
    for bad in ("../" + "a" * 61, "A" * 64, "a" * 63):
        with pytest.raises(files.FileCheckFailed):
            files.file_path(root, "c0ffee00-0000-4000-8000-000000000001", bad)


FIRST_POINTS = ["after_mkdir", "after_dir_sync"]


@pytest.mark.parametrize("nth", range(8))
@pytest.mark.parametrize("point", FIRST_POINTS)
def test_a_crash_while_making_the_first_folders_never_leaves_a_committed_row(engine, monkeypatch, point, nth):
    seen = []

    def crash(at):
        if at == point:
            seen.append(at)
            if len(seen) == nth + 1:
                raise Crash(at)

    monkeypatch.setattr(files, "fault", crash)
    try:
        _put(engine)
    except Crash:
        assert _rows(engine) == 0
    else:  # fewer folders than nth: the put completed, and its file is whole
        assert len(seen) <= nth
    monkeypatch.setattr(files, "fault", lambda at: None)
    fid = _put(engine)
    assert _get(engine, fid) == PDF


# --- the root and its marker -----------------------------------------------------------------------------------------

def test_the_root_is_named_by_the_hex_of_the_database_name():
    assert files.root_name("books") == "db-626f6f6b73"
    assert files.root_name("Books") != files.root_name("books")
    assert files.root_name("bücher") == "db-" + "bücher".encode("utf-8").hex()


def test_the_first_file_writes_the_root_marker_with_the_books_identity_and_name(engine):
    _put(engine)
    with engine.connect() as conn:
        root, books, db = files.root_dir(conn), books_id(conn), files.database_name(conn)
    raw = (root / files.ROOT_MARKER).read_bytes()
    assert raw == (json.dumps({"books_id": books, "database": db, "format": 1}, sort_keys=True,
                              separators=(",", ":")) + "\n").encode()


def test_books_with_no_files_start_and_make_no_root(engine):
    files.check_root(engine)
    assert not files.files_dir().exists()


def test_books_with_files_start_when_their_root_is_theirs(engine):
    _put(engine)
    files.check_root(engine)


def _root(engine) -> Path:
    with engine.connect() as conn:
        return files.root_dir(conn)


def test_the_engine_refuses_to_start_when_the_root_folder_is_missing(engine):
    _put(engine)
    root = _root(engine)
    root.rename(root.with_name("moved-away"))
    with pytest.raises(FilesRefused, match="are not in files/db-"):
        files.check_root(engine)


def test_the_engine_refuses_to_start_when_the_marker_is_missing(engine):
    _put(engine)
    (_root(engine) / files.ROOT_MARKER).unlink()
    with pytest.raises(FilesRefused, match="marker is missing"):
        files.check_root(engine)


def _rewrite_marker(engine, **change) -> None:
    path = _root(engine) / files.ROOT_MARKER
    doc = {**json.loads(path.read_text()), **change}
    path.write_text(json.dumps(doc))


def test_the_engine_refuses_to_start_when_the_marker_names_other_books(engine):
    _put(engine)
    _rewrite_marker(engine, books_id="00000000-0000-4000-8000-000000000000")
    with pytest.raises(FilesRefused, match="belong to other books"):
        files.check_root(engine)


def test_the_engine_refuses_to_start_when_the_books_were_renamed_outside_restore(engine):
    _put(engine)
    _rewrite_marker(engine, database="old_books")
    with pytest.raises(FilesRefused, match="renamed from old_books to"):
        files.check_root(engine)


@pytest.mark.parametrize("change", [{"format": 2}, {"extra": "x"}, {"books_id": 7}])
def test_the_engine_refuses_a_marker_that_is_not_the_shape_it_writes(engine, change):
    _put(engine)
    _rewrite_marker(engine, **change)
    with pytest.raises(FilesRefused, match="not one this engine wrote"):
        files.check_root(engine)


def test_a_put_into_a_root_marked_for_other_books_is_refused(engine):
    _put(engine)
    _rewrite_marker(engine, books_id="00000000-0000-4000-8000-000000000000")
    with engine.begin() as conn, pytest.raises(Refusal) as e:
        files.put(company_ctx(conn), PDF, "application/pdf", ("sales_invoice", "inv-0002"))
    assert e.value.code == "files_not_these_books"


@pytest.fixture
def case_twins():
    """Two new books on the test server, named only by case apart (`..._Case_test` and `..._case_test`)."""
    from erpclaw_sales.demo import seed_demo

    from conftest import PG_URL

    tag = secrets.token_hex(3)
    names = [f"erpclaw_{tag}_Case_test", f"erpclaw_{tag}_case_test"]
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        for n in names:
            conn.exec_driver_sql(f'CREATE DATABASE "{n}"')
    engines = [make_engine(make_url(PG_URL).set(database=n).render_as_string(hide_password=False)) for n in names]
    for e in engines:
        seed_demo(e)
    yield engines
    for e in engines:
        e.dispose()
    with admin.connect() as conn:
        for n in names:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
    admin.dispose()


def test_two_books_named_only_by_case_apart_keep_separate_roots_and_read_only_their_own(case_twins):
    upper, lower = case_twins
    a = _put(upper, data=PDF + b"upper\n")
    b = _put(lower, data=PDF + b"lower\n")
    assert _root(upper) != _root(lower) and _root(upper).is_dir() and _root(lower).is_dir()
    assert _get(upper, a) == PDF + b"upper\n" and _get(lower, b) == PDF + b"lower\n"
    for e in case_twins:
        files.check_root(e)  # each root is the one its own name encodes: both start
    with pytest.raises(Refusal):
        _get(upper, b)


def test_a_hand_made_root_naming_a_case_twin_refuses_start_where_names_ignore_case(engine):
    _put(engine)
    root = _root(engine)
    if not Path(str(root).swapcase()).exists():
        pytest.skip("this file system tells names apart by case")
    with engine.connect() as conn:
        db = files.database_name(conn)
    fake = root.parent / "db-handmade"
    fake.mkdir()
    (fake / files.ROOT_MARKER).write_bytes(files._marker(books_id_of(engine), db.swapcase()))
    with pytest.raises(FilesRefused, match="differ only in case"):
        files.check_root(engine)


def books_id_of(engine) -> str:
    with engine.connect() as conn:
        return books_id(conn)


# --- the database keeps the rows -------------------------------------------------------------------------------------

def test_an_update_of_a_file_row_is_refused_by_the_database(engine):
    fid = _put(engine)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
        conn.execute(m.file_object.update().where(m.file_object.c.id == fid).values(size=1))


def test_truncate_is_refused_by_the_database(engine):
    _put(engine)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
        conn.exec_driver_sql("TRUNCATE file_object CASCADE")


def test_a_row_kept_as_long_as_the_books_can_never_be_deleted(engine):
    fid = _put(engine)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="only after its retention date"):
        conn.execute(m.file_object.delete().where(m.file_object.c.id == fid))


def _insert_dated(engine, retention: dt.date, voucher_id: str = "inv-old") -> str:
    """A row with a retention date, inserted as the owner (no engine path sets one in this release)."""
    fid = _put(engine, document=("sales_invoice", voucher_id))
    r = _row(engine, fid)
    new = {**r, "id": f"{fid[:-4]}dead", "voucher_id": voucher_id + "-dated", "retention_until": retention}
    with engine.begin() as conn:
        conn.execute(m.file_object.insert().values(new))
    return new["id"]


def test_a_row_not_yet_past_its_date_is_refused_and_one_past_it_is_deleted_with_its_audit_row(engine):
    with engine.connect() as conn:
        utc_today = conn.exec_driver_sql("SELECT (now() AT TIME ZONE 'UTC')::date").scalar()
    due = _insert_dated(engine, utc_today)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="only after its retention date"):
        conn.execute(m.file_object.delete().where(m.file_object.c.id == due))
    past = _insert_dated(engine, utc_today - dt.timedelta(days=1), "inv-past")
    with engine.begin() as conn:
        conn.execute(m.file_object.delete().where(m.file_object.c.id == past))
        audit = conn.execute(select(m.audit_log).where(m.audit_log.c.action == "file_retired")).mappings().one()
    detail = json.loads(audit["detail"])
    assert detail["file_id"] == past and audit["document_type"] == "sales_invoice"
    assert audit["document_id"] == "inv-past-dated" and set(detail) == {"file_id", "sha256", "size"}


def test_the_retention_date_is_compared_in_utc_whatever_the_sessions_time_zone(engine):
    with engine.connect() as conn:
        utc_today = conn.exec_driver_sql("SELECT (now() AT TIME ZONE 'UTC')::date").scalar()
    fid = _insert_dated(engine, utc_today)  # due tomorrow in UTC: never deletable today, in any zone
    for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "UTC"):  # UTC+14 and UTC-11: a day apart locally
        with engine.begin() as conn, pytest.raises(DBAPIError, match="only after its retention date"):
            conn.exec_driver_sql(f"SET LOCAL TIME ZONE '{zone}'")
            conn.execute(m.file_object.delete().where(m.file_object.c.id == fid))


def test_deleting_a_company_that_has_a_book_lifetime_file_is_refused(engine):
    """The foreign key refuses (RESTRICT); a cascade would reach the row and be refused by its trigger instead."""
    cid = "c0ffee00-0000-4000-8000-000000000001"
    with engine.begin() as conn:  # a company with nothing else, so only its file can hold it
        conn.execute(m.company.insert().values(id=cid, name="Bare Co", functional_currency="USD",
                                               fiscal_year_start_month=1, created_at=dt.datetime.now(dt.timezone.utc)))
        files.put(company_ctx(conn, company_id=cid), PDF, "application/pdf", DOC)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="foreign key"):
        conn.execute(m.company.delete().where(m.company.c.id == cid))


def test_the_runtime_role_may_insert_file_rows_only(engine):
    from erpclaw_core.db import APPEND

    assert m.RUNTIME["file_object"] == APPEND


# --- the retention job -----------------------------------------------------------------------------------------------

YESTERDAY = dt.date(2000, 1, 1)  # long past in any zone


def _bare_company(engine, name: str) -> str:
    cid = "c0ffee00-0000-4000-8000-" + hashlib.sha256(name.encode()).hexdigest()[:12]
    with engine.begin() as conn:
        conn.execute(m.company.insert().values(id=cid, name=name, functional_currency="USD", fiscal_year_start_month=1,
                                               created_at=dt.datetime.now(dt.timezone.utc)))
    return cid


def _dated_only(engine, company_id: str, data: bytes, voucher_id: str = "old") -> tuple[str, Path]:
    """Bytes on disk named only by a row past its retention date. No engine path writes a dated row in this release,
    so the owner writes the file and the row the way put does."""
    sha = hashlib.sha256(data).hexdigest()
    fid = secrets.token_hex(18)
    with engine.begin() as conn:
        path = files.file_path(files._ensure_root(conn), company_id, sha)
        files._make_dirs(path.parent)
        files._write_durably(path, data)
        conn.execute(m.file_object.insert().values(
            id=fid, company_id=company_id, sha256=sha, size=len(data), media_type="application/pdf",
            voucher_type="sales_invoice", voucher_id=voucher_id, created_at=dt.datetime.now(dt.timezone.utc),
            retention_until=YESTERDAY))
    return fid, path


def _ids(engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(select(m.file_object.c.id)).scalars())


def test_retire_deletes_a_row_past_its_date_and_removes_bytes_no_row_names(engine):
    with engine.connect() as conn:
        demo = company_ctx(conn).company_name
    cid = _bare_company(engine, "Old Co")
    fid, path = _dated_only(engine, cid, PDF + b"old\n")
    keep = _put(engine, company=demo)
    out = retention.retire(engine)
    assert (out["retired"], out["files_removed"]) == (1, 1)
    assert not path.exists() and fid not in _ids(engine)
    assert _get(engine, keep, company=demo) == PDF  # a book-lifetime file is untouched


def test_retire_keeps_bytes_another_row_of_the_same_company_names(engine):
    cid = _bare_company(engine, "Old Co")
    data = PDF + b"twice\n"
    fid, path = _dated_only(engine, cid, data)
    with engine.begin() as conn:
        files.put(company_ctx(conn, company_id=cid), data, "application/pdf", ("sales_invoice", "current"))
    out = retention.retire(engine)
    assert (out["retired"], out["files_removed"]) == (1, 0) and path.exists()


def test_retiring_one_companys_row_leaves_bytes_another_company_names(engine):
    """Still named is checked by sha256 alone, across the books' companies."""
    x, y = _bare_company(engine, "X Co"), _bare_company(engine, "Y Co")
    data = PDF + b"shared\n"
    _, x_path = _dated_only(engine, x, data)
    with engine.begin() as conn:
        yid = files.put(company_ctx(conn, company_id=y), data, "application/pdf", ("sales_invoice", "y-1"))
    out = retention.retire(engine)
    assert (out["retired"], out["files_removed"]) == (1, 0)
    assert x_path.exists() and _get_as(engine, y, yid) == data


def _get_as(engine, company_id, fid) -> bytes:
    with engine.connect() as conn:
        return files.get(company_ctx(conn, company_id=company_id), fid)


def test_retire_skips_a_past_date_file_a_message_names_and_counts_it(engine):
    from erpclaw_core.outbox import Attachment, enqueue

    from test_outbox_core import SENDER

    a_id, a_path = _dated_only(engine, _company_id(engine), PDF + b"a\n", "a-old")
    b_id, b_path = _dated_only(engine, _company_id(engine), PDF + b"b\n", "b-old")
    with engine.begin() as conn:
        enqueue(company_ctx(conn), "deliver_message", "ar@acme.example", "Old", "Old file.", None, sender=SENDER,
                attachments=(Attachment(a_id, "old.pdf", hashlib.sha256(PDF + b"a\n").hexdigest()),))
    out = retention.retire(engine)
    assert (out["retired"], out["skipped_named_by_messages"]) == (1, 1)
    assert a_id in _ids(engine) and b_id not in _ids(engine)
    assert a_path.exists() and not b_path.exists()


def _company_id(engine) -> str:
    with engine.connect() as conn:
        return company_ctx(conn).company_id


def test_retire_removes_leftovers_older_than_a_day_and_keeps_named_and_new_ones(engine):
    import os

    fid = _put(engine)
    path = _path(engine, fid)
    stray = path.with_name("e" * 64)
    stray.write_bytes(b"left by an interrupted put")
    tmp = path.with_name(".tmp-interrupted")
    tmp.write_bytes(b"partial")
    fresh = path.with_name("f" * 64)
    fresh.write_bytes(b"too new to judge")
    old = (dt.datetime.now() - dt.timedelta(days=2)).timestamp()
    for p in (path, stray, tmp):
        os.utime(p, (old, old))
    out = retention.retire(engine)
    assert out["leftovers_removed"] == 2
    assert path.exists() and fresh.exists() and not stray.exists() and not tmp.exists()


# a put of the same bytes interleaved with retire at each step: never a row naming missing bytes, never bytes a
# row names removed

def _invariant(engine) -> None:
    with engine.connect() as conn:
        root = files.root_dir(conn)
        for r in conn.execute(select(m.file_object)).mappings():
            data = files.file_path(root, r["company_id"], r["sha256"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == r["sha256"]


@pytest.mark.parametrize("point", ["retire_after_delete", "retire_after_lock", "retire_after_check"])
def test_a_put_interleaved_with_retire_at_each_step_never_breaks_a_row(engine, monkeypatch, point):
    import threading

    lone = _bare_company(engine, "Lone Co")
    data = PDF + b"raced\n"
    _dated_only(engine, lone, data)
    errors, threads = [], []

    def put_same():
        try:
            with engine.begin() as conn:
                files.put(company_ctx(conn, company_id=lone), data, "application/pdf", ("sales_invoice", "new"))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    def at(where):
        if where != point:
            return
        if where == "retire_after_delete":
            put_same()  # no lock held here: the put completes first
        else:
            t = threading.Thread(target=put_same)
            t.start()
            t.join(0.5)
            assert t.is_alive()  # it waits for the bytes' lock that retire holds
            threads.append(t)

    monkeypatch.setattr(files, "fault", at)
    retention.retire(engine)
    for t in threads:
        t.join(10)
    assert errors == []
    with engine.connect() as conn:
        rows = conn.execute(select(m.file_object.c.voucher_id).where(m.file_object.c.company_id == lone)).scalars()
        assert sorted(rows) == ["new"]
    _invariant(engine)


def test_retire_waits_for_a_put_of_the_same_bytes_that_holds_their_lock(engine, monkeypatch):
    import threading

    lone = _bare_company(engine, "Lone Two")
    data = PDF + b"first\n"
    _dated_only(engine, lone, data)
    done = []

    def at(where):
        if where == "before_insert":  # the put holds the bytes' lock: retire, started now, must wait for it
            t = threading.Thread(target=lambda: done.append(retention.retire(engine)))
            t.start()
            t.join(0.5)
            assert t.is_alive()  # waiting for the bytes' lock the put holds
            at.thread = t

    monkeypatch.setattr(files, "fault", at)
    with engine.begin() as conn:
        files.put(company_ctx(conn, company_id=lone), data, "application/pdf", ("sales_invoice", "new"))
    monkeypatch.setattr(files, "fault", lambda where: None)
    at.thread.join(10)
    assert done and done[0]["files_removed"] == 0
    _invariant(engine)
