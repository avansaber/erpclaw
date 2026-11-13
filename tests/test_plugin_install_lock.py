"""Installs and approval read sets share the chain-head lock, even for an absent row."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from sqlalchemy import event, select, text

from erpclaw_core import ledger, models as m, plugins
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.setup import create_company


PATHS = ("install_into", "install_record", "install_plugin")


@pytest.fixture
def books(engine):
    # Bound every competing transaction, including the engine-owned CLI path.
    def timeouts(conn):
        conn.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        conn.exec_driver_sql("SET LOCAL statement_timeout = '8s'")

    event.listen(engine, "begin", timeouts)
    with engine.begin() as conn:
        plugins.disable_plugin(conn, "email_folder")
    try:
        yield engine
    finally:
        event.remove(engine, "begin", timeouts)


def install(engine, path):
    try:
        if path == "install_plugin":
            plugins.install_plugin(engine, "email")
        else:
            with engine.begin() as conn:
                getattr(plugins, path)(conn, plugins.REGISTRY.plugins["email"])
    except Refusal as exc:
        return exc.code
    return "installed"


def row(conn):
    return conn.execute(select(m.plugin_install).where(m.plugin_install.c.name == "email")).mappings().first()


def waits_for(holder, future):
    """Observe the competing session blocked by this connection, using only two connections."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        waiting = holder.execute(text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted "
            "AND pg_backend_pid() = ANY(pg_blocking_pids(pid)))"
        )).scalar_one()
        if waiting:
            assert not future.done(), "the competing transaction finished while its blocker held the lock"
            return
        if future.done():
            pytest.fail(f"the competing transaction did not wait: {future.result()}")
        time.sleep(0.01)
    pytest.fail("the competing transaction never waited for the chain-head holder")


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_first_install_waits_for_chain_head(books, path, end):
    with ThreadPoolExecutor(max_workers=1) as pool:
        with books.connect() as holder:
            ledger.lock_chain(company_ctx(holder))
            assert row(holder) is None
            future = pool.submit(install, books, path)
            try:
                waits_for(holder, future)
                assert row(holder) is None
            finally:
                getattr(holder, end)()
        assert future.result(timeout=10) == "installed"
    with books.connect() as conn:
        assert row(conn)["status"] == "installed"


@pytest.mark.parametrize("path", PATHS)
def test_install_alone_duplicate_and_reinstall(books, path):
    assert install(books, path) == "installed"
    with books.connect() as conn:
        original = dict(row(conn))
    assert install(books, path) == "plugin_installed"
    with books.begin() as conn:
        assert dict(row(conn)) == original
        plugins.uninstall_plugin(conn, "email")
    assert install(books, path) == "installed"
    with books.connect() as conn:
        assert row(conn)["status"] == "installed"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("first", ["approval", "install"])
@pytest.mark.parametrize("existing", [False, True])
def test_approval_and_install_lock_order(books, monkeypatch, path, first, existing):
    if existing:
        assert install(books, path) == "installed"
    expected = "plugin_installed" if existing else "installed"
    real_checks = plugins._install_checks

    # Exercise both an absent row and an existing row's duplicate refusal.
    # Holding at the checks proves the head was taken FIRST.
    def approval():
        with books.begin() as conn:
            ledger.lock_chain(company_ctx(conn))
            assert conn.execute(select(m.plugin_install.c.name).where(
                m.plugin_install.c.name == "email").with_for_update()).scalar_one() == "email"
        return "approved"

    for _ in range(3):
        if first == "approval":
            with ThreadPoolExecutor(max_workers=1) as pool:
                with books.connect() as holder:
                    ledger.lock_chain(company_ctx(holder))
                    locked = holder.execute(select(m.plugin_install.c.name).where(
                        m.plugin_install.c.name == "email").with_for_update()).scalar_one_or_none()
                    assert locked == ("email" if existing else None)
                    future = pool.submit(install, books, path)
                    try:
                        waits_for(holder, future)
                    finally:
                        holder.rollback()
                assert future.result(timeout=10) == expected
        else:
            checking, release = threading.Event(), threading.Event()
            state = {}

            def held_checks(conn, plugin):
                state["conn"] = conn
                checking.set()
                assert release.wait(10), "the test did not release the installer"
                real_checks(conn, plugin)

            with monkeypatch.context() as patch:
                patch.setattr(plugins, "_install_checks", held_checks)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    installing = pool.submit(install, books, path)
                    try:
                        assert checking.wait(10), "the installer did not reach its checks"
                        approving = pool.submit(approval)
                        # The installer thread is paused, so its connection can
                        # observe the other session without concurrent use.
                        waits_for(state["conn"], approving)
                    finally:
                        release.set()
                    assert installing.result(timeout=10) == expected
                    assert approving.result(timeout=10) == "approved"
        if not existing:
            with books.begin() as conn:
                plugins.uninstall_plugin(conn, "email")


@pytest.mark.parametrize("path", PATHS)
def test_install_waits_for_another_company(books, path):
    cid = create_company(books, "Other Books")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with books.connect() as holder:
            ledger.lock_chain(company_ctx(holder, company_name="Other Books"))
            assert holder.execute(select(m.posting_chain_head.c.company_id).where(
                m.posting_chain_head.c.company_id == cid)).scalar_one() == cid
            future = pool.submit(install, books, path)
            try:
                waits_for(holder, future)
            finally:
                holder.rollback()
        assert future.result(timeout=10) == "installed"


@pytest.mark.parametrize("path", ["install_into", "install_record"])
def test_install_holds_chain_head_until_callers_transaction_ends(books, path):
    with ThreadPoolExecutor(max_workers=1) as pool:
        with books.connect() as holder:
            getattr(plugins, path)(holder, plugins.REGISTRY.plugins["email"])

            def approval():
                with books.begin() as conn:
                    ledger.lock_chain(company_ctx(conn))
                    assert row(conn)["status"] == "installed"
                return "approved"

            future = pool.submit(approval)
            try:
                waits_for(holder, future)
            finally:
                holder.commit()
        assert future.result(timeout=10) == "approved"
