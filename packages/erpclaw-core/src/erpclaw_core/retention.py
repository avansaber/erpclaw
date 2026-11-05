"""The owner's retention job for stored files: rows past their retention date that no message names are deleted
(the database writes each delete's audit row), then the bytes no remaining row names are removed under their lock,
then leftovers. It runs with the owner's credential: the runtime role cannot delete a stored file's row."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import literal_column, select
from sqlalchemy.engine import Engine

from . import files, models as m
from .common import now

OWNER_ONLY = True  # the owner's command: under the runtime role it fails with permission denied

UTC_TODAY = literal_column("(now() AT TIME ZONE 'UTC')::date")
LEFTOVER_AGE = dt.timedelta(days=1)


def _remove_unnamed(engine: Engine, sha256: str, paths: list[Path]) -> int:
    """Remove the copies of the bytes at `paths`, only while holding their lock and no row names them."""
    with engine.begin() as conn:
        files.lock_bytes(conn, sha256)
        files.fault("retire_after_lock")
        if files.named(conn, sha256):
            return 0
        files.fault("retire_after_check")
        return sum(files._unlink(p) for p in paths)


def retire(engine: Engine) -> dict:
    """The owner's retention job: delete the rows past their retention date that no message names (each delete
    writes its audit row), then remove the bytes no remaining row names, then leftovers older than a day that no row
    names. Run with the owner's credential; the runtime role cannot delete a row."""
    fo, att = m.file_object, m.outbox_attachment
    with engine.begin() as conn:
        due = conn.execute(select(fo.c.id, fo.c.company_id, fo.c.sha256).where(fo.c.retention_until < UTC_TODAY)
                           .order_by(fo.c.id)).all()
        held = set(conn.execute(select(att.c.file_id).where(att.c.file_id.in_([d.id for d in due]))).scalars())
        gone = [d for d in due if d.id not in held]
        if gone:
            conn.execute(fo.delete().where(fo.c.id.in_([d.id for d in gone])))
        root = files.root_dir(conn)
    files.fault("retire_after_delete")
    by_sha: dict[str, set[str]] = {}
    for d in gone:
        by_sha.setdefault(d.sha256, set()).add(d.company_id)
    removed = sum(_remove_unnamed(engine, sha, [files.file_path(root, c, sha) for c in sorted(companies)])
                  for sha, companies in sorted(by_sha.items()))
    leftovers = _remove_leftovers(engine, root)
    with engine.connect() as conn:
        server = files.server_identity(conn)
        marks = files.clean_restore_marks(server, lambda name: _database_exists(conn, name))
    return {"retired": len(gone), "skipped_named_by_messages": len(held), "files_removed": removed,
            "leftovers_removed": leftovers, "restore_marks": marks}


def _database_exists(conn, name: str | None) -> bool:
    """Whether the server has the database now; asked at the moment of each decision, never from an earlier list."""
    return name is not None and conn.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(n)s",
                                                     {"n": name}).first() is not None


def _remove_leftovers(engine: Engine, root: Path) -> int:
    """Files older than a day that no row names, and temporary names an interrupted put left."""
    if not root.is_dir():
        return 0
    cutoff = (now() - LEFTOVER_AGE).timestamp()
    count = 0
    for company in sorted(p for p in root.iterdir() if p.is_dir()):
        for prefix in sorted(p for p in company.iterdir() if p.is_dir()):
            for path in sorted(prefix.iterdir()):
                if not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
                if path.name.startswith(".tmp-"):
                    count += files._unlink(path)
                elif files.SHA_NAME.match(path.name):
                    count += _remove_unnamed(engine, path.name, [path])  # where it lies, even misfiled
    return count
