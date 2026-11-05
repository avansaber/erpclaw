"""First-use races must not drop an independent assistant-store caller."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import stat
import threading

import pytest
from erpclaw_assistant import store


@pytest.mark.parametrize("stale_ready", [False, True])
def test_two_first_callers_initialize_one_private_store_and_keep_both_rows(tmp_path, monkeypatch, stale_ready):
    target = tmp_path / "synthetic" / "assistant.sqlite"
    target.parent.mkdir()
    monkeypatch.setattr(store, "path", lambda: target)
    monkeypatch.setattr(store, "_ready", {str(target)} if stale_ready else set())
    both_absent = threading.Barrier(2)
    real_exists = Path.exists
    local = threading.local()
    parent = threading.current_thread()

    def exists(path):
        present = real_exists(path)
        if path == target and threading.current_thread() is not parent and not getattr(local, "checked", False):
            local.checked = True
            assert not present
            # Both filesystem observations precede either O_EXCL attempt.
            both_absent.wait(timeout=10)
        return present

    monkeypatch.setattr(Path, "exists", exists)

    def record(number):
        store.record_demand("synthetic-company", "c", f"synthetic-{number}", f"synthetic request {number}")
        return store.version()

    # result() propagates any worker exception: no unhandled-thread warning can be counted as a pass.
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(record, number) for number in range(2)]
        versions = [future.result(timeout=30) for future in futures]
    assert versions == [store.VERSION, store.VERSION]
    assert {row[3] for row in store.demand()} == {"synthetic-0", "synthetic-1"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_private_file_creation_failure_does_not_fall_through_to_sqlite_creation(tmp_path, monkeypatch):
    target = tmp_path / "synthetic" / "assistant.sqlite"
    monkeypatch.setattr(store, "path", lambda: target)
    monkeypatch.setattr(store, "_ready", set())
    real_open = os.open

    def denied(path, flags, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError("synthetic private-create refusal")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", denied)
    def unexpected_connect(*args, **kwargs):
        pytest.fail("failed private creation must not be replaced with sqlite implicit creation")
    monkeypatch.setattr(store.sqlite3, "connect", unexpected_connect)
    with pytest.raises(PermissionError, match="synthetic private-create refusal"):
        store.version()
    assert not target.exists()
