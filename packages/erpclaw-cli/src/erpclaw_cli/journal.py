"""The chat window's approval journal: one entry per approval in its data folder's approvals/ (mode 0700), so an
approval whose reply is lost can be checked again, never sent twice at once and never sent without a whole entry.

An entry is published atomically and locked before it has its name, the name given by a link that never replaces an
existing entry (a folder sync that fails afterwards takes the name back, or, if that fails too, leaves it published
and sent): a temporary file (".tmp-" and random letters,
mode 0600) is created, locked exclusively, written and synced, then linked as "<approval id>.json" and the folder
synced; the lock stays on the same file through the naming, so no other window can take a published entry before
its own window does, and only a whole entry ever has a .json name. The lock is held while the approval is sent and
its reply awaited; on the engine's answer the entry is removed and the folder synced. Recovery reads only .json
entries it can lock without waiting and that are still there once locked, for the same books; a temporary file is
never sent and is never another window's to finish, so it is removed only when a day old.

Imports nothing of the engine: the window keeps its own journal.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TEMP = ".tmp-"
DAY = 86400


class NotRecorded(Exception):
    """A step of publishing failed: the approval must not be sent."""


class AlreadySending(Exception):
    """Another window holds this approval's entry: it is that window's to send, never sent twice."""


@dataclass
class Entry:
    path: Path
    data: dict
    fd: int


def folder() -> Path:
    """The window's data folder's approvals/: $ERPCLAW_HOME, else ./var, as the engine's own data root."""
    home = os.environ.get("ERPCLAW_HOME")
    return (Path(home) if home else Path.cwd() / "var") / "approvals"


def _sync_folder(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Journal:
    def __init__(self, where: Path, books: str):
        self.folder, self.books, self.held = Path(where), books, []

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        for entry in list(self.held):
            self.keep(entry)

    def _ready(self) -> None:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)

    def publish(self, approval_id: str, digest: str, conversation_id: str) -> Entry:
        """Steps 1 to 3: the whole entry under its name, locked by this window. An entry already there for this
        approval is never replaced: one no other window holds is taken over (an earlier window's unconfirmed approval
        of this same card); one another window holds raises AlreadySending. Any failure raises NotRecorded and leaves
        no .json entry behind for this window to be told 'not sent' about."""
        temp = fd = None
        final = self.folder / f"{approval_id}.json"
        try:
            self._ready()
            if final.exists():
                return self._take_over(final, approval_id, digest)
            name = TEMP + "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
            temp = self.folder / name
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # before it has its name, held through the naming
            data = {"approval_id": approval_id, "digest": digest, "conversation_id": conversation_id,
                    "books": self.books, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            body = json.dumps(data).encode()
            if os.write(fd, body) != len(body):
                raise OSError("short write")
            os.fsync(fd)
            try:  # a link names the same file (and its lock) and never replaces an existing name
                os.link(temp, final)
            except FileExistsError:
                raise AlreadySending(approval_id) from None  # another window named it a moment ago
            try:
                _sync_folder(self.folder)
            except OSError:
                try:  # not durably published: the name taken back, never left whole to be sent later
                    os.unlink(final)
                except OSError:
                    pass  # the name cannot be taken back: whole and locked by this window, it is sent as published
                else:
                    raise
            try:
                os.unlink(temp)  # the entry lives on under its name; a temporary name left is removed when a day old
            except OSError:
                pass
            temp = None
        except AlreadySending:
            self._abandon(temp, fd)
            raise
        except OSError as e:
            self._abandon(temp, fd)
            raise NotRecorded(str(e)) from None
        entry = Entry(final, data, fd)
        self.held.append(entry)
        return entry

    @staticmethod
    def _abandon(temp: Path | None, fd: int | None) -> None:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass
        if fd is not None:
            os.close(fd)

    def _take_over(self, final: Path, approval_id: str, digest: str) -> Entry:
        """An entry already named for this approval: this window's own if no other holds it and it is for this
        card and these books; otherwise it is another window's to send."""
        entry = self._lockable(final)
        if entry is None:
            raise AlreadySending(approval_id)
        if (entry.data.get("approval_id"), entry.data.get("digest"), entry.data.get("books")) != \
                (approval_id, digest, self.books):
            self.keep(entry)
            raise NotRecorded("an entry of that name records another approval")
        return entry

    def owns(self, entry: Entry) -> bool:
        """Whether the entry is still there under its name and is the file this window holds: checked before every
        send, so an approval is never sent after its entry is gone or replaced."""
        try:
            return entry in self.held and os.fstat(entry.fd).st_ino == os.stat(entry.path).st_ino
        except OSError:
            return False

    def finish(self, entry: Entry) -> None:
        """The engine answered: the entry is removed, the folder synced, the lock released."""
        try:
            os.unlink(entry.path)
            _sync_folder(self.folder)
        except FileNotFoundError:
            pass
        finally:
            self._release(entry)

    def keep(self, entry: Entry) -> None:
        """No answer: the entry stays for the next check, and this window lets it go."""
        self._release(entry)

    def _release(self, entry: Entry) -> None:
        if entry in self.held:
            self.held.remove(entry)
            os.close(entry.fd)  # closing the descriptor releases its lock

    def _lockable(self, path: Path) -> Entry | None:
        """The entry at `path`, locked by this window, if no other window holds it and it is still there."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        try:
            if os.fstat(fd).st_ino != os.stat(path).st_ino:
                raise FileNotFoundError
            data = json.loads(os.read(fd, 1 << 16))
        except (OSError, ValueError):
            os.close(fd)  # removed (or replaced) by its own window after the listing, or not an entry
            return None
        entry = Entry(path, data, fd)
        self.held.append(entry)
        return entry

    def recover(self) -> list[Entry]:
        """The unconfirmed approvals of these books this window may check now, oldest first, each locked; the
        temporary files a day old removed. An entry another window holds, or of other books, is left alone."""
        if not self.folder.is_dir():
            return []
        found = []
        for path in sorted(self.folder.iterdir()):
            if path.name.startswith(TEMP):
                try:
                    if time.time() - path.stat().st_mtime > DAY:
                        path.unlink()
                except OSError:
                    pass
                continue
            if path.suffix != ".json":
                continue
            entry = self._lockable(path)
            if entry is None:
                continue
            if entry.data.get("books") != self.books:
                self.keep(entry)
                continue
            found.append(entry)
        return sorted(found, key=lambda e: e.data.get("time") or "")
