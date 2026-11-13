"""Core delivery scheduling, with PostgreSQL locks and a loopback-only SMTP fake."""
import datetime as dt
import smtplib
import socketserver
import threading
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url

from erpclaw_core import models as m, outbox
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import disable_plugin, install_plugin

from slot_support import replace_filler


SENDER = outbox.SenderSnapshot("books@example.test", "Books", "reply@example.test", None)
BODY = "The approved plain-text message.\n"


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        self.wfile.write(b"220 test SMTP\r\n")
        while line := self.rfile.readline():
            verb = line.split(None, 1)[0].upper()
            if verb == b"MAIL" and self.server.temporary:
                self.server.temporary = False
                self.wfile.write(b"421 try later\r\n")
            elif verb == b"DATA":
                self.wfile.write(b"354 send message\r\n")
                body = []
                while (line := self.rfile.readline()) != b".\r\n":
                    assert line, "SMTP session ended during DATA"
                    body.append(line[1:] if line.startswith(b"..") else line)
                self.server.messages.append(BytesParser(policy=policy.default).parsebytes(b"".join(body)))
                self.wfile.write(b"250 accepted\r\n")
            elif verb == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")


@pytest.fixture
def mail(engine, runtime_login, monkeypatch):
    """Only the registered transport is fake; scheduling and sending use runtime grants."""
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
    # SMTP now owns a table: install its runtime grants as the production CLI does.
    install_plugin(engine, "email", runtime_role=make_url(runtime_login).username)
    runtime = create_engine(runtime_login)
    now = SimpleNamespace(value=dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(outbox, "clock", lambda: now.value)
    monkeypatch.setenv("ERPCLAW_OUTBOX_SWEEP_SECONDS", "0.02")
    with socketserver.TCPServer(("127.0.0.1", 0), SMTPHandler) as server:
        server.messages = []
        server.temporary = False
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()

        def send_message(settings, msg, mark_data):
            assert msg.attachments == ()
            message = EmailMessage()
            message["From"] = f"{msg.from_name} <{msg.from_address}>"
            message["To"] = msg.recipient
            message["Reply-To"] = msg.reply_to
            message["Subject"] = msg.subject
            message["Message-ID"] = msg.message_id
            message.set_content(msg.body)
            with smtplib.SMTP(*server.server_address, timeout=5) as smtp:
                smtp.ehlo()
                code, _ = smtp.mail(msg.from_address)
                if code == 421:
                    return outbox.SendOutcome("before_data", "temporary", code, "", "unreachable")
                assert code == 250 and smtp.rcpt(msg.recipient)[0] == 250
                if not mark_data():
                    return outbox.SendOutcome("before_data", "lost_ownership", None, "")
                assert smtp.data(message.as_bytes(policy=policy.SMTP))[0] == 250
            return outbox.SendOutcome("after_data", "accepted", 250, "accepted")

        replace_filler(monkeypatch, "send_message", send_message)
        try:
            yield SimpleNamespace(engine=runtime, server=server, clock=now)
        finally:
            server.shutdown()
            worker.join(5)
            runtime.dispose()


def queue(engine):
    with engine.begin() as conn:
        return outbox.enqueue(company_ctx(conn), "send_message", "customer@example.test", "Approved message",
                              BODY, None, sender=SENDER, attachments=())


def row(engine, oid):
    with engine.connect() as conn:
        return dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())


def attempts(engine, oid):
    with engine.connect() as conn:
        return conn.execute(select(m.outbox_attempt).where(m.outbox_attempt.c.outbox_id == oid)
                            .order_by(m.outbox_attempt.c.started_at)).mappings().all()


def until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    assert predicate(), "The sweeper did not make progress"


def test_due_rows_select_only_queued_messages_at_or_before_now(engine, monkeypatch):
    now = dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(outbox, "clock", lambda: now)
    expected = set()
    for status, deadline in [("queued", None), ("queued", now - dt.timedelta(seconds=1)), ("queued", now),
                             ("queued", now + dt.timedelta(seconds=1)), ("sending", None), ("accepted", None),
                             ("unknown", None), ("failed", None)]:
        oid = queue(engine)
        with engine.begin() as conn:
            conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(status=status, next_try_at=deadline))
        if status == "queued" and (deadline is None or deadline <= now):
            expected.add(oid)
    with engine.connect() as conn:
        assert {r["id"] for r in outbox.due_rows(conn)} == expected


def test_sweeper_sends_a_new_due_row_without_a_card(mail):
    sweeper = outbox.OutboxSweeper(mail.engine)
    sweeper.start()
    try:
        # Enqueue after startup so this requires the periodic thread.
        oid = queue(mail.engine)
        until(lambda: row(mail.engine, oid)["status"] == "accepted")
        (message,) = mail.server.messages
        assert message.get_content().replace("\r\n", "\n") == BODY
        assert message["Message-ID"] == row(mail.engine, oid)["message_id"]
        assert message["To"] == "customer@example.test" and not message.is_multipart()
        (attempt,) = attempts(mail.engine, oid)
        assert attempt["data_at"] is not None and attempt["result"] == "accepted"
    finally:
        sweeper.stop()


def test_running_sweeper_retries_temporary_smtp_failure_after_backoff(mail, monkeypatch):
    failed = threading.Event()

    def record_failure(point):
        if point == "after_result":
            failed.set()

    monkeypatch.setattr(outbox, "fault", record_failure)
    mail.server.temporary = True
    oid = queue(mail.engine)
    sweeper = outbox.OutboxSweeper(mail.engine)
    sweeper.start()
    try:
        # Catch-up makes the first attempt; only the periodic thread can retry it.
        assert failed.is_set()
        pending = row(mail.engine, oid)
        assert pending["status"] == "queued" and pending["tries"] == 1
        assert pending["next_try_at"] == mail.clock.value + outbox.BACKOFF[0]
        assert mail.server.messages == []
        threading.Event().wait(0.08)  # multiple passes, still before the backoff deadline
        assert len(attempts(mail.engine, oid)) == 1
        mail.clock.value = pending["next_try_at"]
        # No direct delivery call, card, restart, or second start() can drive this retry.
        until(lambda: row(mail.engine, oid)["status"] == "accepted")
        first, second = attempts(mail.engine, oid)
        assert first["result"] == "temporary" and first["data_at"] is None
        assert second["result"] == "accepted" and second["data_at"] is not None
        assert len(mail.server.messages) == 1
    finally:
        sweeper.stop()


def test_acceptance_before_record_result_recovers_unknown_without_resending(mail, monkeypatch, caplog):
    crashed = threading.Event()

    class Killed(BaseException):
        pass

    def kill(point):
        if point == "after_answer" and not crashed.is_set():
            crashed.set()
            raise Killed("synthetic-secret-must-stay-private")

    monkeypatch.setattr(outbox, "fault", kill)
    sweeper = outbox.OutboxSweeper(mail.engine)
    sweeper.start()
    try:
        oid = queue(mail.engine)
        assert crashed.wait(5)
        assert row(mail.engine, oid)["status"] == "sending"
        assert len(mail.server.messages) == 1
        mail.clock.value += outbox.LEASE
        until(lambda: row(mail.engine, oid)["status"] == "unknown")
        for _ in range(3):
            outbox.deliver_pending(mail.engine)
        assert row(mail.engine, oid)["status"] == "unknown"
        (attempt,) = attempts(mail.engine, oid)
        assert attempt["data_at"] is not None and attempt["finished_at"] is None
        assert row(mail.engine, oid)["current_attempt_id"] == attempt["id"]
        assert "Nothing will be resent unless you ask" in outbox.owner_text(row(mail.engine, oid))
        assert len(mail.server.messages) == 1
        assert "Outbox delivery pass did not finish" in caplog.text
        assert "synthetic-secret" not in caplog.text
    finally:
        sweeper.stop()


def test_two_sweepers_with_the_same_due_snapshot_send_each_row_once(mail, monkeypatch):
    sweepers = [outbox.OutboxSweeper(mail.engine) for _ in range(2)]
    due_rows = outbox.due_rows
    rendezvous = threading.Barrier(2)
    snapshots = []
    ids = set()

    def same_snapshot(conn):
        rows = due_rows(conn)
        if ids and {r["id"] for r in rows} == ids:
            snapshots.append({r["id"] for r in rows})
            rendezvous.wait(5)
        return rows

    for sweeper in sweepers:
        sweeper.start()
    try:
        # Commit a batch together, then make both queries return the same snapshot.
        with monkeypatch.context() as patch:
            patch.setattr(outbox, "due_rows", same_snapshot)
            with mail.engine.begin() as conn:
                for _ in range(3):
                    ids.add(outbox.enqueue(company_ctx(conn), "send_message", "customer@example.test", "Batch",
                                           BODY, None, sender=SENDER, attachments=()))
            until(lambda: all(row(mail.engine, oid)["status"] == "accepted" for oid in ids))
        assert snapshots == [ids, ids]
        assert len(mail.server.messages) == len(ids)
        assert {str(msg["Message-ID"]) for msg in mail.server.messages} == {
            row(mail.engine, oid)["message_id"] for oid in ids}
        assert all(len(attempts(mail.engine, oid)) == 1 for oid in ids)
    finally:
        for sweeper in sweepers:
            sweeper.stop()


def test_pass_recovers_expired_sending_rows_before_querying_due_rows(mail):
    unmarked, marked = queue(mail.engine), queue(mail.engine)
    assert outbox.claim(mail.engine, unmarked)
    aid = outbox.claim(mail.engine, marked)
    assert outbox.mark_data(mail.engine, marked, aid)
    mail.clock.value += outbox.LEASE
    results = outbox.deliver_pending(mail.engine)
    assert {r["id"] for r in results} == {unmarked}
    assert row(mail.engine, unmarked)["status"] == "accepted"
    assert row(mail.engine, marked)["status"] == "unknown"
    assert attempts(mail.engine, unmarked)[0]["result"] == "abandoned"
    with mail.engine.connect() as conn:
        assert conn.execute(select(m.outbox.c.id).where(m.outbox.c.status == "sending")).all() == []
    assert len(mail.server.messages) == 1


def test_pass_does_not_take_over_an_unexpired_attempt(mail):
    oid = queue(mail.engine)
    aid = outbox.claim(mail.engine, oid)
    assert outbox.deliver_pending(mail.engine) == []
    assert row(mail.engine, oid)["status"] == "sending"
    assert row(mail.engine, oid)["current_attempt_id"] == aid
    assert len(attempts(mail.engine, oid)) == 1 and mail.server.messages == []


def test_start_catches_up_and_stop_interrupts_a_long_interval(mail, monkeypatch):
    monkeypatch.setenv("ERPCLAW_OUTBOX_SWEEP_SECONDS", "3600")
    oid = queue(mail.engine)
    sweeper = outbox.OutboxSweeper(mail.engine)
    sweeper.start()
    try:
        assert row(mail.engine, oid)["status"] == "accepted"
        workers = [t for t in threading.enumerate() if t.name == "erpclaw-outbox"]
        assert len(workers) == 1
        sweeper.start()  # idempotent: no second worker
        assert [t for t in threading.enumerate() if t.name == "erpclaw-outbox"] == workers
    finally:
        started = time.monotonic()
        sweeper.stop()
    assert time.monotonic() - started < 1
    assert all(not worker.is_alive() for worker in workers)
    sweeper.stop()  # safe before a restart and on repeated cleanup
    later = queue(mail.engine)
    sweeper.start()
    try:
        assert row(mail.engine, later)["status"] == "accepted"
    finally:
        sweeper.stop()


def test_sweeper_records_pass_errors_and_keeps_running(mail, monkeypatch, caplog):
    recover = outbox.recover
    calls = 0

    def unavailable(engine):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("synthetic-secret-must-stay-private")
        return recover(engine)

    monkeypatch.setattr(outbox, "recover", unavailable)
    oid = queue(mail.engine)
    sweeper = outbox.OutboxSweeper(mail.engine)
    sweeper.start()
    try:
        until(lambda: row(mail.engine, oid)["status"] == "accepted")
        assert calls >= 3 and len(mail.server.messages) == 1
        assert "Outbox delivery pass did not finish" in caplog.text
        assert "synthetic-secret" not in caplog.text
    finally:
        sweeper.stop()


def test_released_sweeper_never_runs_a_pass(mail, as_released, monkeypatch):
    oid = queue(mail.engine)

    def forbidden(engine):
        pytest.fail("A planned sweeper ran a pass")

    monkeypatch.setattr(outbox, "deliver_pending", forbidden)
    sweeper = outbox.OutboxSweeper(mail.engine)
    sweeper.start()
    try:
        assert not [t for t in threading.enumerate() if t.name == "erpclaw-outbox"]
        assert row(mail.engine, oid)["status"] == "queued"
    finally:
        sweeper.stop()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "1e100", "not-seconds"])
def test_invalid_host_interval_is_refused_before_delivery(monkeypatch, value):
    monkeypatch.setenv("ERPCLAW_OUTBOX_SWEEP_SECONDS", value)
    with pytest.raises(ValueError, match="positive finite"):
        outbox.OutboxSweeper(None)


def test_default_host_interval_is_one_minute(monkeypatch):
    monkeypatch.delenv("ERPCLAW_OUTBOX_SWEEP_SECONDS", raising=False)
    assert outbox.OutboxSweeper(None).interval == 60
