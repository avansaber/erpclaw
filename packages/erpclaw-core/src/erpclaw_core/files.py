"""The file store: a document's files, immutable and content-addressed, under the books' own file root.

A file reaches the disk before any row names it: the bytes are written to a temporary name, synced, renamed to their
content name and the folder synced, and only then is the row inserted, in the caller's transaction. Every folder made
on the way is synced, and its parent after it. So a committed row always names a file that reached the disk, and an
interruption leaves at most a file no row names.

The root of the books' files is named after the books' database name, and holds a marker with the books' identity
and that name, so the engine never reads another books' files under a name that matches by accident."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Connection, Engine

from . import models as m
from .common import Ctx, Refusal, new_id, now
from .db import books_id, data_root, read_only
from .plugins import BooksRefused

log = logging.getLogger(__name__)

MEDIA_TYPES = ("application/pdf",)
MAX_SIZE = 10 * 1024 * 1024
ROOT_MARKER = "ROOT.json"
ROOT_FORMAT = 1
LOCK_CLASS = 7_303_615  # the advisory lock class of a stored file's bytes; the key is taken from its sha256
UNSAFE = "The file could not be saved safely."
ALTERED = "The stored file has been altered or lost."
SHA_NAME = re.compile(r"^[0-9a-f]{64}$")
COMPANY_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # a company id: a UUID


class FilesRefused(BooksRefused):
    """The books' files are missing or belong to other books: the engine does not open them."""


def fault(point: str) -> None:
    """Called after each durable step of a put; a test replaces it to crash there."""


# --- where the files are -----------------------------------------------------------------------------------------

def files_dir() -> Path:
    return data_root() / "files"


def root_name(database: str) -> str:
    """The root folder of a database's files: `db-` and the lowercase hex of the name's exact UTF-8 bytes, so two
    distinct names never share a folder, even where file names ignore case."""
    return "db-" + database.encode("utf-8").hex()


def database_name(conn: Connection) -> str:
    return conn.exec_driver_sql("SELECT current_database()").scalar()


def root_dir(conn: Connection) -> Path:
    return files_dir() / root_name(database_name(conn))


def file_path(root: Path, company_id: str, sha256: str) -> Path:
    """A stored file's path under `root`, only for a company id of UUID shape and a sha256 of 64 lowercase hex: a
    name from an untrusted backup never leaves the root."""
    if not isinstance(company_id, str) or not COMPANY_NAME.match(company_id):
        raise FileCheckFailed(f"a stored file's company id is not one the engine makes: {company_id!r:.60}")
    if not isinstance(sha256, str) or not SHA_NAME.match(sha256):
        raise FileCheckFailed(f"a stored file's sha256 is not 64 lowercase hex characters: {sha256!r:.80}")
    path = root / company_id / sha256[:2] / sha256
    if not path.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise FileCheckFailed("a stored file's path leaves its root")
    return path


# --- durable writes ------------------------------------------------------------------------------------------------

def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_dirs(path: Path) -> None:
    """Make each missing folder on the path, from the top: each new folder synced, then its parent."""
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for folder in reversed(missing):
        try:
            folder.mkdir()
        except FileExistsError:  # made at the same moment by another put; synced below all the same
            pass
        fault("after_mkdir")
        _sync_dir(folder)
        _sync_dir(folder.parent)
        fault("after_dir_sync")


def _home_problem() -> str | None:
    """The engine never makes a folder above its home: the home may be made, its parent must already exist. So the
    highest entry an interrupted put can leave unsynced is the home's own, in the home's parent. Returns what is
    wrong, in words for the operator (it names paths), or None."""
    home = Path(os.path.abspath(data_root()))
    if home.parent.is_dir():
        return None
    return (f"The engine's home {home} cannot be made: its folder {home.parent} does not exist. Create it, or set "
            "ERPCLAW_HOME to a folder that does.")


def _home_ready() -> None:
    """For a put, which runs inside an approved card's commit and so can reach the owner in chat: the owner reads
    business words, the operator's words with the paths go to the log."""
    problem = _home_problem()
    if problem:
        log.warning(problem)
        raise Refusal("ERPClaw's storage folder isn't set up yet, so the file could not be saved.", "files_home_missing")


def _sync_path(path: Path) -> None:
    """Sync a file or folder, then each folder above it up to the engine home, then the home's parent: so what an
    interrupted put or restore left behind (made, renamed, never synced, the home itself included) has reached the
    disk before a row names it or a database is published. The one routine both use, with one bound. Only the home's
    parent may refuse to be opened (it is not the engine's)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    home = os.path.abspath(data_root())
    folder = Path(os.path.abspath(path.parent))
    while str(folder) != home and folder != folder.parent:
        _sync_dir(folder)
        folder = folder.parent
    _sync_dir(folder)  # the home
    try:
        _sync_dir(folder.parent)  # the home's own entry
    except PermissionError:
        pass
    fault("after_path_sync")


def _write_durably(path: Path, data: bytes) -> None:
    """Write the bytes under a temporary name, sync, rename to `path`, sync the folder."""
    tmp = path.with_name(f".tmp-{new_id()}")
    with open(tmp, "xb") as f:
        f.write(data)
        f.flush()
        fault("after_write")
        os.fsync(f.fileno())
    fault("after_file_sync")
    os.replace(tmp, path)
    fault("after_rename")
    _sync_dir(path.parent)
    fault("after_folder_sync")


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- the root marker -----------------------------------------------------------------------------------------------

def _marker(books: str, database: str) -> bytes:
    return (_canonical({"books_id": books, "database": database, "format": ROOT_FORMAT}) + "\n").encode("utf-8")


def read_marker(root: Path) -> dict | None:
    """The root's marker, when it is exactly the shape the engine writes; None when there is none; a refusal when
    it is not that shape."""
    path = root / ROOT_MARKER
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        doc = None
    if (not isinstance(doc, dict) or set(doc) != {"books_id", "database", "format"} or doc["format"] != ROOT_FORMAT
            or not isinstance(doc["books_id"], str) or not isinstance(doc["database"], str)):
        raise FilesRefused(f"The marker {path} is not one this engine wrote; restore the books' files, or point "
                           "the engine at the books they belong to.")
    return doc


def _marker_problem(root: Path, books: str, database: str) -> str | None:
    """What is wrong with the root for these books, in words for the owner; None when it is theirs."""
    folder = f"files/{root.name}"
    if not root.is_dir():
        return (f"The files of these books are not in {folder}; restore them, or point the engine at the books "
                "they belong to.")
    doc = read_marker(root)
    if doc is None:
        return (f"The files of these books are not in {folder} (its marker is missing); restore them, or point the "
                "engine at the books they belong to.")
    if doc["books_id"] != books:
        return f"The files in {folder} belong to other books."
    if doc["database"] != database:
        return (f"The books were renamed from {doc['database']} to {database}; rename the files folder to match, "
                "or rename the database back.")
    return None


def _ensure_root(conn: Connection) -> Path:
    """The books' root, made with its marker before its first file; refused when it belongs to other books."""
    database, books = database_name(conn), books_id(conn)
    root = files_dir() / root_name(database)
    if not (root / ROOT_MARKER).exists():
        _home_ready()
        _make_dirs(root)
        _write_durably(root / ROOT_MARKER, _marker(books, database))
    _sync_path(root / ROOT_MARKER)  # a marker an interrupted first put left is made durable too
    problem = _marker_problem(root, books, database)
    if problem:
        raise Refusal(problem, "files_not_these_books")
    return root


def _case_twins(database: str) -> list[str]:
    """Hand-made roots naming a database that differs from this one only in case, on a file system that ignores
    case: a root whose folder is not the one its marker's name encodes. Two real books named apart only by case
    have their own folders, each the one its name encodes, and both start."""
    base = files_dir()
    if not base.is_dir() or not Path(str(base).swapcase()).exists():  # a case-sensitive file system
        return []
    twins = []
    for root in base.glob("db-*"):
        try:
            doc = read_marker(root)
        except FilesRefused:
            continue
        if (doc and doc["database"] != database and doc["database"].lower() == database.lower()
                and root.name != root_name(doc["database"])):
            twins.append(doc["database"])
    return sorted(twins)


def check_root(engine: Engine) -> None:
    """At startup: books that have stored files open only when their root is theirs: present, marked, with their identity and name. Books
    with no file yet open, and their root is made with their first file. Never deletes anything."""
    with read_only(engine) as conn:
        if not inspect(conn).has_table("file_object"):
            return
        if conn.execute(select(m.file_object.c.id).limit(1)).first() is None:
            return
        database, books = database_name(conn), books_id(conn)
    root = files_dir() / root_name(database)
    problem = _marker_problem(root, books, database)
    if problem:
        raise FilesRefused(problem)
    twins = _case_twins(database)
    if twins:
        raise FilesRefused(f"The files folder holds a root for {', '.join(twins)} beside {database}, names that "
                           "differ only in case on this file system; remove or rename the root that is not theirs.")


# --- put and get ---------------------------------------------------------------------------------------------------

def lock_bytes(conn: Connection, sha256: str) -> None:
    """One lock per stored bytes in these books, held to the end of the transaction: a put and the retention job
    never act on the same bytes at once."""
    key = int.from_bytes(bytes.fromhex(sha256)[:4], "big", signed=True)
    conn.execute(text("SELECT pg_advisory_xact_lock(:c, :k)"), {"c": LOCK_CLASS, "k": key})


def _render_inputs(value, path: str = "render_inputs") -> None:
    if not isinstance(value, dict):
        raise Refusal(f"{path} must be a mapping of names to text.", "render_inputs_shape")
    for k, v in value.items():
        if not isinstance(k, str):
            raise Refusal(f"{path} has a key that is not text: {k!r}.", "render_inputs_shape")
        if isinstance(v, dict):
            _render_inputs(v, f"{path}.{k}")
        elif not isinstance(v, str):
            raise Refusal(f"{path}.{k} must be text or a mapping, not {type(v).__name__}.", "render_inputs_shape")


def put(ctx: Ctx, data: bytes, media_type: str, document: tuple[str, str], *,
        render_inputs: dict | None = None) -> str:
    """Store the bytes of a document's file and return its row's id; the same bytes for the same document return
    the existing row. Runs in the caller's transaction: the row commits with it, after the file reached the disk."""
    if not isinstance(data, (bytes, bytearray)):
        raise Refusal("A stored file is bytes.", "file_not_bytes")
    if media_type not in MEDIA_TYPES:
        raise Refusal(f"A file of type {media_type} cannot be stored; only {', '.join(MEDIA_TYPES)}.",
                      "file_type_refused")
    if not 0 < len(data) <= MAX_SIZE:
        raise Refusal("A stored file holds at least one byte and at most 10 MB.", "file_size_refused")
    if (not isinstance(document, tuple) or len(document) != 2 or not all(isinstance(x, str) and x for x in document)
            or len(document[0]) > 40 or len(document[1]) > 36):
        raise Refusal("A stored file belongs to one document: (voucher type, voucher id).", "file_document")
    if render_inputs is not None:
        _render_inputs(render_inputs)
    data = bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    voucher_type, voucher_id = document
    lock_bytes(ctx.conn, sha)
    same = ctx.conn.execute(select(m.file_object.c.id).where(
        m.file_object.c.company_id == ctx.company_id, m.file_object.c.voucher_type == voucher_type,
        m.file_object.c.voucher_id == voucher_id, m.file_object.c.sha256 == sha)).scalar()
    if same:
        return same
    try:
        root = _ensure_root(ctx.conn)
        path = file_path(root, ctx.company_id, sha)
        if not _intact(path, sha, len(data)):  # stored once; a damaged copy is written again
            _make_dirs(path.parent)
            _write_durably(path, data)
        _sync_path(path)  # a copy an interrupted put left is made durable too, before any row names it
    except OSError:
        raise Refusal(UNSAFE, "file_not_saved") from None
    fault("before_insert")
    fid = new_id()
    ctx.conn.execute(m.file_object.insert().values(
        id=fid, company_id=ctx.company_id, sha256=sha, size=len(data), media_type=media_type,
        voucher_type=voucher_type, voucher_id=voucher_id, created_at=now(), retention_until=None,
        render_inputs=None if render_inputs is None else _canonical(render_inputs)))
    return fid


def _intact(path: Path, sha256: str, size: int) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return len(data) == size and hashlib.sha256(data).hexdigest() == sha256


def row(ctx: Ctx, file_id: str) -> dict:
    """The company's file row, or a refusal; another company's file is never found."""
    r = ctx.conn.execute(select(m.file_object).where(m.file_object.c.id == file_id,
                                                     m.file_object.c.company_id == ctx.company_id)).mappings().first()
    if r is None:
        raise Refusal("No such stored file.", "file_not_found")
    return dict(r)


def get(ctx: Ctx, file_id: str) -> bytes:
    """The file's bytes, only while their sha256 and size still match the row."""
    r = row(ctx, file_id)
    path = file_path(root_dir(ctx.conn), r["company_id"], r["sha256"])
    try:
        data = path.read_bytes()
    except OSError:
        raise Refusal(ALTERED, "file_altered") from None
    if len(data) != r["size"] or hashlib.sha256(data).hexdigest() != r["sha256"]:
        raise Refusal(ALTERED, "file_altered")
    return data


def named(conn: Connection, sha256: str) -> bool:
    """Whether any row in these books, of any company, still names the bytes."""
    return conn.execute(select(func.count()).select_from(m.file_object)
                        .where(m.file_object.c.sha256 == sha256)).scalar() > 0


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _sync_dir(path.parent)
    return True


# --- backup and restore of the files -------------------------------------------------------------------------------

RESTORE_MARKER = "RESTORE.json"
RESTORE_PREFIX = ".restore-"
MARK_AGE = dt.timedelta(hours=1)


class FileCheckFailed(Exception):
    """A file a backup or restore needs is missing, or differs from its row."""


def referenced(rows: list[dict]) -> list[tuple[str, str, int]]:
    """Each stored file the rows name, once: (company, sha256, size)."""
    return sorted({(r["company_id"], r["sha256"], int(r["size"])) for r in rows})


def _inside(path: Path, within: Path) -> None:
    """Refuse a source that is a symlink, or that resolves outside `within` (a folder link included)."""
    if (within.is_symlink() or path.is_symlink()
            or not os.path.realpath(path).startswith(os.path.realpath(within) + os.sep)):
        raise FileCheckFailed(f"the stored file {path.parent.parent.name}/{path.name} is not a plain file inside "
                              "its folder")


def _copy_checked(src: Path, dest: Path, sha256: str, size: int, within: Path) -> None:
    _inside(src, within)
    try:
        data = src.read_bytes()
    except OSError:
        raise FileCheckFailed(f"the stored file {dest.parent.parent.name}/{sha256} is missing") from None
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise FileCheckFailed(f"the stored file {dest.parent.parent.name}/{sha256} differs from its row")
    _make_dirs(dest.parent)  # made from the nearest existing folder down, never above it
    with open(dest, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if not _intact(dest, sha256, size):
        raise FileCheckFailed(f"the copy of {dest.parent.parent.name}/{sha256} differs from its row")


def back_up(source_root: Path, rows: list[dict], dest: Path) -> int:
    """Copy every file the rows name into `dest`, each copy checked against its row; returns how many."""
    refs = referenced(rows)
    for company, sha, size in refs:
        _copy_checked(file_path(source_root, company, sha), file_path(dest, company, sha), sha, size, source_root)
    return len(refs)


def server_identity(conn: Connection) -> str:
    """This PostgreSQL server's own identity, never its host name."""
    return str(conn.exec_driver_sql("SELECT system_identifier FROM pg_control_system()").scalar())


def _lock_marker(path: Path, blocking: bool = False):
    """An exclusive lock on a restore marker, held while the returned descriptor is open; None when another holds it."""
    import fcntl

    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


class RestoreFiles:
    """A restore's files: checked in the backup, staged beside the roots under a marker this restore holds locked,
    published as the new database's root by one rename that never replaces a root, and unmarked once the database
    is published. The live books' root is never written."""

    def __init__(self, backup_files: Path, rows: list[dict], database: str, books: str):
        self.source, self.refs, self.database, self.books = backup_files, referenced(rows), database, books
        self.staging: Path | None = None
        self.root: Path | None = None
        self._fd = None

    def check_backup(self) -> None:
        """Every file the restored rows name is in the backup, whole: else nothing is staged."""
        for company, sha, size in self.refs:
            _inside(file_path(self.source, company, sha), self.source)
            if not _intact(file_path(self.source, company, sha), sha, size):
                raise FileCheckFailed(f"the backup's file {company}/{sha} is missing or differs from its row")

    def stage(self, server: str, restore_id: str) -> None:
        base = files_dir()
        problem = _home_problem()  # restore is the operator's command: the paths go in its refusal
        if problem:
            raise FileCheckFailed(problem)
        _make_dirs(base)
        _sync_path(base)  # a files folder or home an interrupted restore left behind is made durable too
        target = base / root_name(self.database)
        if target.exists():
            raise FileCheckFailed(f"files/{target.name} already exists; restore never writes over a root")
        self.staging = base / f"{RESTORE_PREFIX}{new_id()}"
        self.staging.mkdir()
        _sync_dir(base)
        marker = self.staging / RESTORE_MARKER
        _write_durably(marker, (_canonical({"database": self.database, "format": 1, "restore_id": restore_id,
                                            "system_identifier": server}) + "\n").encode("utf-8"))
        self._fd = _lock_marker(marker, blocking=True)
        for company, sha, size in self.refs:
            dest = file_path(self.staging, company, sha)
            _make_dirs(dest.parent)
            _copy_checked(file_path(self.source, company, sha), dest, sha, size, self.source)
            _sync_dir(dest.parent)
        _write_durably(self.staging / ROOT_MARKER, _marker(self.books, self.database))
        fault("restore_files_staged")

    def publish(self) -> None:
        base = files_dir()
        target = base / root_name(self.database)
        if target.exists():  # a rename onto a folder that holds anything fails; onto none, it never replaces
            raise FileCheckFailed(f"files/{target.name} already exists; restore never writes over a root")
        os.rename(self.staging, target)
        _sync_dir(base)
        self.root, self.staging = target, None
        fault("restore_files_published")

    def finish(self) -> None:
        """The database is published: the root is an ordinary root from now on."""
        if self.root is not None:
            _unlink(self.root / RESTORE_MARKER)
        self.release()

    def abandon(self) -> None:
        """The restore failed for certain: remove what it staged or published, which it alone holds."""
        import shutil

        for folder in (self.staging, self.root):
            if folder is not None and folder.exists():
                shutil.rmtree(folder)
                _sync_dir(folder.parent)
        self.staging = self.root = None
        self.release()

    def release(self) -> None:
        """Let go of the marker's lock (the process ending does the same)."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _restore_mark(folder: Path) -> dict | None:
    try:
        doc = json.loads((folder / RESTORE_MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or set(doc) != {"database", "format", "restore_id", "system_identifier"}:
        return None
    return doc


def database_of(root: Path) -> str | None:
    """The database name a root's folder encodes, or None for a folder the engine did not name."""
    if not root.name.startswith("db-"):
        return None
    try:
        return bytes.fromhex(root.name[3:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def clean_restore_marks(server: str, database_exists) -> dict:
    """Remove only what one of this server's own restores marked and is provably no longer running: a marked staging
    folder, or a marked root whose database does not exist on this server; a marked root whose database exists only
    loses its marker. A root without a marker is never removed; those whose database is not on this server are
    reported. `database_exists(name)` asks the server at that moment: a list read earlier could miss a restore that
    published its database since."""
    import shutil

    base = files_dir()
    out = {"removed": [], "unmarked": [], "roots_without_database": []}
    if not base.is_dir():
        return out
    cutoff = (now() - MARK_AGE).timestamp()
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or not (folder.name.startswith(RESTORE_PREFIX) or folder.name.startswith("db-")):
            continue
        marker = folder / RESTORE_MARKER
        if not marker.exists():
            db = database_of(folder)
            if db is not None and not database_exists(db):
                out["roots_without_database"].append(folder.name)
            continue
        doc = _restore_mark(folder)
        try:
            old = marker.stat().st_mtime < cutoff
            fd = _lock_marker(marker) if doc and doc["system_identifier"] == server and old else None
        except FileNotFoundError:  # its restore finished meanwhile
            continue
        if fd is None:  # not this server's, too new, or its restore still holds the lock
            continue
        try:
            fault("cleanup_after_lock")
            # under the lock, the marker must still be the one read, and the server is asked afresh: a restore that
            # published and finished since the first look has removed its marker or made its database exist
            held, now_there = os.fstat(fd), (marker.stat() if marker.exists() else None)
            if (now_there is None or (held.st_ino, held.st_dev) != (now_there.st_ino, now_there.st_dev)
                    or _restore_mark(folder) != doc):
                continue
            if folder.name.startswith("db-") and database_exists(database_of(folder)):
                _unlink(marker)
                out["unmarked"].append(folder.name)
            else:
                shutil.rmtree(folder)
                _sync_dir(base)
                out["removed"].append(folder.name)
        finally:
            os.close(fd)
    return out


def check_restored(root: Path, rows: list[dict]) -> list[str]:
    """What is wrong with a restored root's files against its rows; empty when every file is whole."""
    return [f"the file {company}/{sha} is missing or differs from its row" for company, sha, size in referenced(rows)
            if not _intact(file_path(root, company, sha), sha, size)]
