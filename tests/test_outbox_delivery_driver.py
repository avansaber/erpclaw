"""The serving engine drains the real outbox without another card or a network connection."""
import datetime as dt
import os
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from erpclaw_assistant.model import ScriptedModel
from erpclaw_cli import main as cli
from erpclaw_core import models as m, outbox
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import disable_plugin, install_plugin

from slot_support import replace_filler


SENDER = outbox.SenderSnapshot("books@acme.example", "Acme Books", "reply@acme.example", None)
BODY = "Hello,\nHere is the approved plain-text message.\n"


class Mailbox:
    """The registered email slot's test transport: the real outbox still owns every step."""

    def __init__(self):
        self.messages = []
        self.workers = []
        self.temporary = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def send_message(self, settings, msg, mark_data):
        self.workers.append(threading.current_thread())
        if self.temporary:
            self.temporary = False
            return outbox.SendOutcome("before_data", "temporary", None, "", reason="unreachable")
        assert mark_data()
        self.messages.append(msg)
        self.entered.set()
        assert self.release.wait(5), "The test did not release the transport"
        return outbox.SendOutcome("after_data", "accepted", 250, "2.0.0 queued")


@pytest.fixture
def serving(engine, runtime_login, monkeypatch, tmp_path):
    """Serve with genuine runtime grants and the email plugin's registered slot replaced only at transport."""
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
    # SMTP now owns a table: install its runtime grants as the production CLI does.
    install_plugin(engine, "email", runtime_role=make_url(runtime_login).username)
    mailbox = Mailbox()
    assert replace_filler(monkeypatch, "send_message", mailbox.send_message) == "email"
    runtime = create_engine(runtime_login)
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", runtime_login)
    monkeypatch.setenv("ERPCLAW_OUTBOX_SWEEP_SECONDS", "0.02")
    monkeypatch.setattr("erpclaw_core.db.make_engine", lambda url: runtime)
    monkeypatch.setattr("erpclaw_assistant.muse.model_from_env", lambda: ScriptedModel([]))
    fake_time = SimpleNamespace(now=dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(outbox, "clock", lambda: fake_time.now)
    state = SimpleNamespace(engine=runtime, mailbox=mailbox, clock=fake_time, run_file=tmp_path / "run.json")
    try:
        yield state
    finally:
        mailbox.release.set()
        runtime.dispose()


def queue(engine):
    with engine.begin() as conn:
        return outbox.enqueue(company_ctx(conn), "send_message", "accounts@customer.example", "Approved message",
                              BODY, None, sender=SENDER, attachments=())


def row(engine, oid):
    with engine.connect() as conn:
        return dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())


def attempts(engine, oid):
    with engine.connect() as conn:
        return conn.execute(select(m.outbox_attempt).where(m.outbox_attempt.c.outbox_id == oid)
                            .order_by(m.outbox_attempt.c.started_at)).mappings().all()


def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    assert predicate(), "The running engine did not make the expected progress"


def serve(state, monkeypatch, while_serving, *, port_mode=False):
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: while_serving())
    if port_mode:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        args = ["serve", "--port", str(port)]
    else:
        args = ["serve", "--run-file", str(state.run_file), "--tcp"]
    return CliRunner().invoke(cli.app, args)


@pytest.mark.parametrize("port_mode", [False, True])
def test_periodic_pass_sends_plain_text_without_a_card(serving, monkeypatch, port_mode):
    state = serving

    def while_serving():
        # Created after startup catch-up; only a periodic pass can find this row.
        oid = queue(state.engine)
        until(lambda: row(state.engine, oid)["status"] == "accepted")
        (msg,) = state.mailbox.messages
        assert (msg.id, msg.message_id, msg.recipient, msg.subject, msg.body, msg.attachments) == (
            oid, f"<{oid}@acme.example>", "accounts@customer.example", "Approved message", BODY, ())
        assert (msg.from_address, msg.from_name, msg.reply_to) == (
            SENDER.from_address, SENDER.from_name, SENDER.reply_to)
        (attempt,) = attempts(state.engine, oid)
        assert attempt["data_at"] is not None and attempt["result"] == "accepted"

    result = serve(state, monkeypatch, while_serving, port_mode=port_mode)
    assert result.exit_code == 0, (result.output, result.exception)
    assert all(not worker.is_alive() for worker in state.mailbox.workers)
    oid = queue(state.engine)
    threading.Event().wait(0.06)
    assert row(state.engine, oid)["status"] == "queued"
    assert len(state.mailbox.messages) == 1


def test_next_periodic_pass_retries_a_temporary_failure(serving, monkeypatch):
    state = serving
    state.mailbox.temporary = True

    def while_serving():
        oid = queue(state.engine)
        until(lambda: row(state.engine, oid)["tries"] == 1)
        pending = row(state.engine, oid)
        assert pending["status"] == "queued" and state.mailbox.messages == []
        # Several sweeps before the deadline must not make another attempt.
        threading.Event().wait(0.06)
        assert len(attempts(state.engine, oid)) == 1
        state.clock.now = pending["next_try_at"]
        until(lambda: row(state.engine, oid)["status"] == "accepted")
        first, second = attempts(state.engine, oid)
        assert first["result"] == "temporary" and first["data_at"] is None
        assert second["result"] == "accepted" and second["data_at"] is not None
        assert [msg.body for msg in state.mailbox.messages] == [BODY]

    result = serve(state, monkeypatch, while_serving)
    assert result.exit_code == 0, (result.output, result.exception)


@pytest.mark.parametrize("crashed_after_claim", [False, True])
def test_startup_recovers_and_sends_before_serving(serving, monkeypatch, crashed_after_claim):
    state = serving
    oid = queue(state.engine)
    if crashed_after_claim:
        assert outbox.claim(state.engine, oid)
        state.clock.now += outbox.LEASE

    def while_serving():
        assert row(state.engine, oid)["status"] == "accepted"
        assert [msg.body for msg in state.mailbox.messages] == [BODY]
        history = attempts(state.engine, oid)
        if crashed_after_claim:
            assert history[0]["result"] == "abandoned" and history[0]["data_at"] is None
        assert history[-1]["result"] == "accepted"

    result = serve(state, monkeypatch, while_serving)
    assert result.exit_code == 0, (result.output, result.exception)


@pytest.mark.parametrize("port_mode", [False, True])
def test_fresh_process_attempts_due_email_before_serving(engine, runtime_login, tmp_path, port_mode):
    """A new interpreter must load the connector before startup catch-up, without a periodic pass."""
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
    install_plugin(engine, "email", runtime_role=make_url(runtime_login).username)
    oid = queue(engine)
    assert row(engine, oid)["status"] == "queued" and attempts(engine, oid) == []
    run_file = tmp_path / "fresh-run.json"
    if port_mode:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        options = ["--port", str(port)]
    else:
        options = ["--run-file", str(run_file), "--tcp"]
    script = textwrap.dedent("""\
        import os
        import sys
        import uvicorn
        from sqlalchemy import select
        from erpclaw_assistant import muse
        from erpclaw_assistant.model import ScriptedModel
        from erpclaw_cli.main import app
        from erpclaw_core import models as m, outbox
        from erpclaw_core.plugins import REGISTRY

        assert not REGISTRY.plugins, "The subprocess must start without preloaded plugins"
        outbox.PLANNED = None
        muse.model_from_env = lambda: ScriptedModel([])

        def before_serving(application, **options):
            with application.app.state.engine.connect() as conn:
                oid = os.environ["STARTUP_OUTBOX_ID"]
                history = conn.execute(select(m.outbox_attempt).where(
                    m.outbox_attempt.c.outbox_id == oid)).mappings().all()
                assert len(history) == 1, "Startup must attempt due email before serving"
                # No sender is configured; the real connector must refuse before DATA.
                assert history[0]["result"] == "permanent"
                assert history[0]["data_at"] is None
                assert conn.execute(select(m.outbox.c.status).where(
                    m.outbox.c.id == oid)).scalar_one() == "failed"
            print("startup attempt checked", flush=True)

        uvicorn.run = before_serving
        app(["serve", *sys.argv[1:]])
    """)
    env = dict(os.environ, ERPCLAW_DATABASE_URL=runtime_login, ERPCLAW_OUTBOX_SWEEP_SECONDS="3600",
               STARTUP_OUTBOX_ID=oid)
    result = subprocess.run([sys.executable, "-c", script, *options], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "startup attempt checked" in result.stdout
    assert row(engine, oid)["status"] == "failed" and len(attempts(engine, oid)) == 1
    assert not run_file.exists()


def test_app_validation_failure_does_not_start_delivery(serving, monkeypatch):
    state = serving
    oid = queue(state.engine)

    def refused(*args, **kwargs):
        raise ValueError("The served app is not valid")

    monkeypatch.setattr("erpclaw_api.pipe.served_app", refused)
    result = serve(state, monkeypatch, lambda: pytest.fail("An invalid app started serving"))
    assert result.exit_code != 0 and isinstance(result.exception, ValueError)
    assert row(state.engine, oid)["status"] == "queued"
    assert attempts(state.engine, oid) == [] and state.mailbox.messages == []
    assert not state.run_file.exists()


def test_crash_after_acceptance_is_unknown_and_never_resent(serving, monkeypatch):
    state = serving
    oid = queue(state.engine)

    class Crash(BaseException):
        pass

    def crash(point):
        if point == "after_answer":
            raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(outbox, "fault", crash)
        with pytest.raises(Crash):
            outbox.deliver_pending(state.engine)
    assert row(state.engine, oid)["status"] == "sending"
    state.clock.now += outbox.LEASE

    def while_serving():
        current = row(state.engine, oid)
        assert current["status"] == "unknown"
        assert "Nothing will be resent unless you ask" in outbox.owner_text(current)
        threading.Event().wait(0.08)
        assert row(state.engine, oid)["status"] == "unknown"
        (attempt,) = attempts(state.engine, oid)
        assert attempt["data_at"] is not None and attempt["finished_at"] is None
        assert current["current_attempt_id"] == attempt["id"]
        assert [msg.body for msg in state.mailbox.messages] == [BODY]

    result = serve(state, monkeypatch, while_serving)
    assert result.exit_code == 0, (result.output, result.exception)


def test_a_pass_error_does_not_kill_periodic_delivery_or_log_secrets(serving, monkeypatch, caplog):
    state = serving
    recover = outbox.recover
    calls = 0

    def unavailable(engine):
        nonlocal calls
        calls += 1
        if calls <= 2:  # startup and the first periodic pass
            raise RuntimeError("synthetic-password-must-stay-private")
        return recover(engine)

    monkeypatch.setattr(outbox, "recover", unavailable)
    oid = queue(state.engine)

    def while_serving():
        until(lambda: row(state.engine, oid)["status"] == "accepted")

    result = serve(state, monkeypatch, while_serving)
    assert result.exit_code == 0, (result.output, result.exception)
    assert calls >= 3 and len(state.mailbox.messages) == 1
    assert "Outbox delivery pass did not finish" in caplog.text
    assert "synthetic-password" not in caplog.text + result.output


@pytest.mark.parametrize("server_error", [False, True])
def test_shutdown_interrupts_the_interval_and_removes_the_run_file(serving, monkeypatch, server_error):
    state = serving
    monkeypatch.setenv("ERPCLAW_OUTBOX_SWEEP_SECONDS", "60")
    workers = []

    def while_serving():
        workers.extend(t for t in threading.enumerate() if t.name == "erpclaw-outbox")
        assert len(workers) == 1
        assert state.run_file.exists()
        if server_error:
            raise RuntimeError("server stopped")

    started = time.monotonic()
    result = serve(state, monkeypatch, while_serving)
    assert result.exit_code == (1 if server_error else 0), (result.output, result.exception)
    assert time.monotonic() - started < 3
    assert all(not t.is_alive() for t in workers)
    assert not state.run_file.exists()


def test_shutdown_waits_for_the_inflight_attempt(serving):
    state = serving
    state.mailbox.release.clear()
    started, stop, stopped = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def run():
        try:
            with outbox.delivery_driver(state.engine):
                started.set()
                assert stop.wait(5)
        except BaseException as exc:
            errors.append(exc)
        finally:
            stopped.set()

    host = threading.Thread(target=run)
    host.start()
    try:
        assert started.wait(3)
        oid = queue(state.engine)
        assert state.mailbox.entered.wait(3)
        stop.set()
        assert not stopped.wait(0.05)
        state.mailbox.release.set()
        assert stopped.wait(3)
        host.join(3)
        assert errors == []
        assert row(state.engine, oid)["status"] == "accepted"
        assert all(not worker.is_alive() for worker in state.mailbox.workers)
    finally:
        stop.set()
        state.mailbox.release.set()
        host.join(5)


def test_released_driver_never_recovers_or_sends(serving, as_released, monkeypatch):
    state = serving
    oid = queue(state.engine)

    def forbidden(*args, **kwargs):
        pytest.fail("The released driver reached delivery or recovery")

    monkeypatch.setattr(outbox, "recover", forbidden)

    def while_serving():
        threading.Event().wait(0.06)
        assert row(state.engine, oid)["status"] == "queued"
        assert attempts(state.engine, oid) == [] and state.mailbox.messages == []

    result = serve(state, monkeypatch, while_serving)
    assert result.exit_code == 0, (result.output, result.exception)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-seconds"])
def test_invalid_host_interval_is_refused_without_starting_delivery(serving, monkeypatch, value):
    state = serving
    oid = queue(state.engine)
    monkeypatch.setenv("ERPCLAW_OUTBOX_SWEEP_SECONDS", value)
    result = serve(state, monkeypatch, lambda: pytest.fail("An invalid interval started serving"))
    assert result.exit_code != 0
    assert row(state.engine, oid)["status"] == "queued" and state.mailbox.messages == []
    assert not state.run_file.exists()
