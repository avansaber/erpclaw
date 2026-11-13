"""Publish listener fields atomically; retain the exact bytes for exit cleanup."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import secrets
import stat


# Test hooks at the validation and cleanup race boundaries.
_after_check = None
_before_unlink = None


def _windows_run_file_security(path: Path) -> None:
    """Platform seam for owner-only creation and reparse checks; a no-op on POSIX.

    Windows needs its native security descriptor in the creating call and native
    reparse-point checks. Refuse there until that implementation is supplied.
    """
    if os.name == "nt":
        raise OSError("Windows run-file security is not implemented.")


def _open_directory(component: str, parent_fd: int | None = None) -> int:
    try:
        return os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        raise ValueError("The run-file folder does not exist.") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("The run-file folder is not a directory or contains a symbolic link.") from None
        raise ValueError("The run-file path could not be checked.") from None


@contextmanager
def _pinned_parent(path: str | os.PathLike[str]) -> Iterator[tuple[int, str]]:
    raw = os.fspath(path)
    path = Path(raw)
    _windows_run_file_security(path)
    name = os.path.split(raw)[1]
    if name in ("", ".", ".."):
        raise ValueError("The run-file name must be a plain file name.")
    # Keep '..' in the walk: a symbolic link before it must still be refused.
    parts = path.parent.parts
    parent_fd = _open_directory(path.anchor or ".")
    try:
        for component in parts[1:] if path.anchor else parts:
            next_fd = _open_directory(component, parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        yield parent_fd, name
    finally:
        os.close(parent_fd)


def _check_final(parent_fd: int, name: str) -> None:
    try:
        mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    except OSError:
        raise ValueError("The run-file path could not be checked.") from None
    if stat.S_ISLNK(mode):
        raise ValueError("The run-file path contains a symbolic link.")
    if stat.S_ISDIR(mode):
        raise ValueError("The run-file name is a directory.")


def check_run_file_path(path: str | os.PathLike[str]) -> None:
    """Refuse symbolic links in any component and absent or non-directory parents."""
    with _pinned_parent(path) as (parent_fd, name):
        _check_final(parent_fd, name)


def write_run_file(path: str | os.PathLike[str], fields: Mapping[str, object]) -> bytes:
    """Write the whole field map once, privately, then replace the target atomically.

    A collision at the temporary name is a failed start, never a retry or an
    overwrite. Only a temporary file this call created is cleaned on failure.
    """
    with _pinned_parent(path) as (parent_fd, name):
        if os.name != "nt":
            import fcntl

        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        try:
            _check_final(parent_fd, name)
            if _after_check is not None:
                _after_check()
            written = (json.dumps(dict(fields), separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
            temporary = f".{name}.{secrets.token_hex(16)}.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=parent_fd)
            published = False
            try:
                try:
                    remaining = memoryview(written)
                    while remaining:
                        count = os.write(fd, remaining)
                        if count == 0:
                            raise OSError("The run file could not be written.")
                        remaining = remaining[count:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                published = True
            finally:
                if not published:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
            return written
        finally:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)


def remove_if_unchanged(path: str | os.PathLike[str], written: bytes) -> None:
    """Best-effort normal-exit cleanup, preserving another engine's replacement."""
    try:
        with _pinned_parent(path) as (parent_fd, name):
            if os.name != "nt":
                import fcntl

            fcntl.flock(parent_fd, fcntl.LOCK_EX)
            try:
                _check_final(parent_fd, name)
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                with os.fdopen(fd, "rb") as source:
                    current = source.read()
                if current == written:
                    if _before_unlink is not None:
                        _before_unlink()
                    os.unlink(name, dir_fd=parent_fd)
            finally:
                fcntl.flock(parent_fd, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
