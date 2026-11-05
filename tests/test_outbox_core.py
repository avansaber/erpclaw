"""The outbox core and the message connector slots: one owner of a send at a time, decided under the outbox row's
lock; a body sent only after its data mark has committed; nothing accepted without that mark; no plugin writes a
status."""
from __future__ import annotations

import datetime as dt
import secrets
import threading

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from erpclaw_core import models as m, outbox, plugins
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.db import DEMO_MARK, EVAL_MARK, make_engine, owner
from erpclaw_core.outbox import (OutboundMessage, ReadRefused, SendOutcome, SenderSnapshot, deliver_pending, enqueue,
                                 owner_text, send_again)
from erpclaw_core.plugins import REGISTRY, PluginRefused, Registry, disable_plugin, install_record

from slot_support import loaded_without_mailers, replace_filler  # every plugin kept, its tables owned
from test_plugins import plug  # tests/ is on the path (pytest rootdir)

SENDER = SenderSnapshot(from_address="books@acme.example", from_name="Acme Books", reply_to="ap@acme.example",
                        read_entry=None)



# --- a fake connector: records what it did, in order, and answers as told -------------------------------------------

class FakeServer:
    def __init__(self):
        self.events: list[str] = []
        self.bodies: list[str] = []
        self.answer = SendOutcome(stage="after_data", kind="accepted", code=250, text="2.0.0 queued", reason=None)
        self.before_data: SendOutcome | None = None  # answer before DATA, instead of marking
        self.raise_at: str | None = None  # "before_mark" or "after_mark"
        self.settings_seen: list[dict | None] = []

    def send_message(self, settings, msg: OutboundMessage, mark_data):
        self.settings_seen.append(settings.one("smtpx_settings"))
        self.events.append("rcpt")
        if self.raise_at == "before_mark":
            raise RuntimeError(f"connection reset; password={SECRET}")
        if self.before_data is not None:
            return self.before_data
        if not mark_data():
            self.events.append("quit")
            return SendOutcome(stage="before_data", kind="lost_ownership", code=None, text="", reason=None)
        if self.raise_at == "after_mark":
            raise RuntimeError(f"connection lost; password={SECRET}")
        self.events.append("data")
        self.bodies.append(msg.body)
        return self.answer

    def message_sender(self, ctx):
        return SENDER


SECRET = "pw-" + secrets.token_hex(8)  # synthetic, built at test time


@pytest.fixture
def smtp(engine, monkeypatch):
    """A fake SMTP plugin filling send_message and message_sender, loaded and installed in these books in place of the
    folder connector (one message connector per set of books)."""
    server = FakeServer()
    p = plug("smtpx", slots={"send_message": server.send_message, "message_sender": server.message_sender})
    r = Registry()
    r.fill(loaded_without_mailers() + [p])
    monkeypatch.setattr(plugins, "REGISTRY", r)
    monkeypatch.setattr(outbox, "REGISTRY", r)
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
        install_record(conn, p)
        conn.exec_driver_sql("CREATE TABLE smtpx_settings (id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36), "
                             "host VARCHAR(100))")
        cid = conn.execute(select(m.company.c.id)).scalars().first()
        conn.exec_driver_sql(f"INSERT INTO smtpx_settings VALUES ('s1', '{cid}', 'mail-1.example')")
    monkeypatch.setitem(r.plugins["smtpx"].manifest.__dict__, "owned_tables", ["smtpx_settings"])
    return server


def queue(engine, recipient="ar@acme.example", subject="Invoice INV-1", body="Please find it attached.",
          sender=SENDER) -> str:
    with engine.begin() as conn:
        return enqueue(company_ctx(conn), "deliver_message", recipient, subject, body, None, sender=sender)


def row(engine, oid: str) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(select(m.outbox).where(m.outbox.c.id == oid)).mappings().one())


def attempts(engine, oid: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(m.outbox_attempt).where(m.outbox_attempt.c.outbox_id == oid)
                                              .order_by(m.outbox_attempt.c.started_at)).mappings()]


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += dt.timedelta(**kw)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(outbox, "clock", c)
    return c


# --- plugin contract v2 ----------------------------------------------------------------------------------------------

def test_the_two_new_slots_are_core_slots_of_cardinality_one():
    assert plugins.SLOTS["send_message"] == "one" and plugins.SLOTS["message_sender"] == "one"
    assert plugins.SLOTS["deliver_message"] == "one"  # v1 unchanged


def test_one_filler_of_each_message_slot_loads_beside_the_folder_connector():
    a = plug("smtpx", slots={"send_message": lambda s, msg, mark: None, "message_sender": lambda ctx: SENDER})
    r = Registry()
    r.fill(loaded_without_mailers() + [a])
    assert "email_folder" in r.slots["deliver_message"] and "smtpx" in r.slots["send_message"]


def test_two_fillers_of_send_message_are_refused():
    a = plug("smtpx", slots={"send_message": lambda s, msg, mark: None})
    b = plug("smtpy", slots={"send_message": lambda s, msg, mark: None})
    with pytest.raises(PluginRefused, match="send_message"):
        Registry().fill(loaded_without_mailers() + [a, b])


def test_a_second_message_connector_in_one_set_of_books_is_refused(engine):
    p = plug("smtpx", slots={"send_message": lambda s, msg, mark: None})
    r = Registry()
    r.fill(loaded_without_mailers() + [p])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(plugins, "REGISTRY", r)
        with pytest.raises(Refusal, match="already send messages through email_folder; disable it first"):
            with engine.begin() as conn:
                install_record(conn, p)


# --- revision 0009 and the runtime grants ----------------------------------------------------------------------------

def test_the_outbox_has_the_five_statuses_and_refuses_delivered(engine):
    oid = queue(engine)
    for status in ("sending", "accepted", "unknown", "failed", "queued"):
        with engine.begin() as conn:
            conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(status=status))
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(m.outbox.update().where(m.outbox.c.id == oid).values(status="delivered"))


def test_the_runtime_grants_are_exactly_the_columns_each_step_writes():
    assert set(m.metadata.tables["outbox"].info["runtime"].update) == {
        "status", "detail", "current_attempt_id", "next_try_at", "tries", "first_failed_at", "accepted_at"}
    assert set(m.metadata.tables["outbox_attempt"].info["runtime"].update) == {
        "data_at", "finished_at", "code", "result", "text"}
    attach = m.metadata.tables["outbox_attachment"].info["runtime"]
    assert attach.insert and not attach.update and not attach.delete


def test_a_queued_message_carries_its_message_id_and_the_approved_sender(engine):
    oid = queue(engine)
    r = row(engine, oid)
    assert r["message_id"] == f"<{oid}@acme.example>"
    assert (r["from_address"], r["from_name"], r["reply_to"]) == ("books@acme.example", "Acme Books",
                                                                  "ap@acme.example")


# --- enqueue refuses what could inject or misaddress ----------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("recipient", "a@x.example\r\nBcc: b@y.example"), ("subject", "Hi\nBcc: b@y.example"), ("body_ok", None),
    ("recipient", "a@x.example, b@y.example"), ("recipient", "Alice <a@x.example>"), ("recipient", "team: a@x.example;"),
    ("recipient", "a@exa\u0000mple.com"), ("recipient", "not an address"),
])
def test_enqueue_refuses_a_header_injection_or_anything_but_one_plain_address(engine, field, value):
    if field == "body_ok":  # a line break in the body is a body, not a header
        assert queue(engine, body="Line one\nLine two")
        return
    with pytest.raises(Refusal):
        queue(engine, **{field: value})


def test_enqueue_refuses_a_line_break_in_the_sender_snapshot(engine):
    bad = SenderSnapshot(from_address="books@acme.example", from_name="Acme\r\nBcc: x@y.example",
                         reply_to="ap@acme.example", read_entry=None)
    with pytest.raises(Refusal):
        queue(engine, sender=bad)


# --- the four steps --------------------------------------------------------------------------------------------------

def test_a_send_marks_then_sends_then_is_accepted(engine, smtp, clock):
    oid = queue(engine)
    deliver_pending(engine)
    r = row(engine, oid)
    assert r["status"] == "accepted" and r["accepted_at"] is not None
    assert smtp.events == ["rcpt", "data"] and len(smtp.bodies) == 1
    (a,) = attempts(engine, oid)
    assert a["data_at"] is not None and a["result"] == "accepted" and a["code"] == 250
    assert deliver_pending(engine) == [] and len(smtp.bodies) == 1  # a further run sends nothing


def test_claim_is_one_owner_at_a_time(engine, smtp, clock):
    oid = queue(engine)
    first = outbox.claim(engine, oid)
    assert first is not None and outbox.claim(engine, oid) is None


def test_two_workers_claiming_together_give_one_owner(engine, smtp, clock):
    oid = queue(engine)
    url = engine.url.render_as_string(hide_password=False)
    engines = [make_engine(url), make_engine(url)]
    start, got = threading.Barrier(2), []

    def worker(e):
        start.wait()
        got.append(outbox.claim(e, oid))

    threads = [threading.Thread(target=worker, args=(e,)) for e in engines]
    [t.start() for t in threads]
    [t.join() for t in threads]
    [e.dispose() for e in engines]
    assert sorted(x is None for x in got) == [False, True]


def test_a_worker_paused_before_its_mark_cannot_send_after_a_takeover(engine, smtp, clock):
    oid = queue(engine)
    a = outbox.claim(engine, oid)
    clock.advance(minutes=6)  # its lease passes
    outbox.recover(engine)
    assert row(engine, oid)["status"] == "queued"
    b = outbox.claim(engine, oid)
    assert b is not None and b != a
    assert outbox.mark_data(engine, oid, a) is False  # the paused worker resumes: no DATA
    assert outbox.mark_data(engine, oid, b) is True
    assert {x["id"]: x["result"] for x in attempts(engine, oid)}[a] == "abandoned"


def test_the_mark_is_given_once_per_attempt(engine, smtp, clock):
    oid = queue(engine)
    a = outbox.claim(engine, oid)
    assert outbox.mark_data(engine, oid, a) is True and outbox.mark_data(engine, oid, a) is False


def test_a_crash_after_the_mark_is_unknown_and_never_resent(engine, smtp, clock):
    oid = queue(engine)
    a = outbox.claim(engine, oid)
    outbox.mark_data(engine, oid, a)
    clock.advance(minutes=6)
    outbox.recover(engine)
    assert row(engine, oid)["status"] == "unknown"
    assert owner_text(row(engine, oid)) == "We don't know whether this was sent. Nothing will be resent unless you ask."
    deliver_pending(engine)
    assert smtp.bodies == []  # never resent by itself


def test_a_late_real_answer_turns_unknown_into_the_truth(engine, smtp, clock):
    oid = queue(engine)
    a = outbox.claim(engine, oid)
    outbox.mark_data(engine, oid, a)
    clock.advance(minutes=6)
    outbox.recover(engine)
    outbox.record_result(engine, oid, a, SendOutcome("after_data", "accepted", 250, "ok", None))
    assert row(engine, oid)["status"] == "accepted"


def test_an_accepted_with_no_mark_is_recorded_failed(engine, smtp, clock):
    oid = queue(engine)
    a = outbox.claim(engine, oid)
    outbox.record_result(engine, oid, a, SendOutcome("after_data", "accepted", 250, "ok", None))
    r = row(engine, oid)
    assert r["status"] == "failed" and "the connector reported a send it did not mark" in r["detail"]


def test_a_result_for_a_replaced_attempt_closes_only_that_attempt(engine, smtp, clock):
    oid = queue(engine)
    a = outbox.claim(engine, oid)
    outbox.mark_data(engine, oid, a)
    clock.advance(minutes=6)
    outbox.recover(engine)
    with engine.begin() as conn:
        send_again(company_ctx(conn), oid)
    b = outbox.claim(engine, oid)
    outbox.record_result(engine, oid, a, SendOutcome("after_data", "accepted", 250, "late", None))
    assert row(engine, oid)["current_attempt_id"] == b and row(engine, oid)["status"] == "sending"
    assert {x["id"]: x["result"] for x in attempts(engine, oid)}[a] == "accepted"


def test_an_exception_before_the_mark_requeues_and_after_it_is_unknown_and_neither_keeps_its_text(engine, smtp,
                                                                                                   clock):
    oid = queue(engine)
    smtp.raise_at = "before_mark"
    deliver_pending(engine)
    r = row(engine, oid)
    assert r["status"] == "queued" and r["tries"] == 1
    clock.advance(minutes=2)
    smtp.raise_at = "after_mark"
    deliver_pending(engine)
    assert row(engine, oid)["status"] == "unknown"
    with engine.connect() as conn:
        dump = " ".join(str(v) for t in ("outbox", "outbox_attempt")
                        for r in conn.execute(text(f"SELECT * FROM {t}")).all() for v in r)
    assert SECRET not in dump and "RuntimeError" in dump


def test_temporary_failures_back_off_then_fail(engine, smtp, clock):
    oid = queue(engine)
    smtp.before_data = SendOutcome("before_data", "temporary", 421, "try later", "unreachable")
    waits = []
    for _ in range(5):
        deliver_pending(engine)
        r = row(engine, oid)
        assert r["status"] == "queued"
        waits.append(r["next_try_at"] - clock())
        clock.advance(hours=7)
    assert waits == [dt.timedelta(minutes=1), dt.timedelta(minutes=5), dt.timedelta(minutes=30),
                     dt.timedelta(hours=2), dt.timedelta(hours=6)]
    deliver_pending(engine)
    r = row(engine, oid)
    assert r["status"] == "failed"
    assert owner_text(r).startswith("Not sent: the mail server could not be reached since ")


@pytest.mark.parametrize("reason,words", [
    ("password_missing", "the mail account's password is not set up"),
    ("password_refused", "the mail account's password was refused"),
    ("certificate_unverified", "the mail server's certificate could not be verified"),
    ("tls_unavailable", "the mail server does not offer a secure connection"),
    ("recipient_refused", "the mail server refused the recipient address (550)"),
])
def test_each_reason_is_worded_by_core(engine, smtp, clock, reason, words):
    oid = queue(engine)
    smtp.before_data = SendOutcome("before_data", "permanent", 550 if reason == "recipient_refused" else None,
                                   "5.7.8 raw server text", reason)
    deliver_pending(engine)
    assert owner_text(row(engine, oid)) == f"Not sent: {words}"


def test_an_unknown_reason_shows_the_code_and_no_connector_words(engine, smtp, clock):
    oid = queue(engine)
    smtp.before_data = SendOutcome("before_data", "permanent", 554, "go away", "made_up_reason")
    deliver_pending(engine)
    t = owner_text(row(engine, oid))
    assert "554" in t and "made_up_reason" not in t


def test_accepted_owner_text_never_says_delivered(engine, smtp, clock):
    oid = queue(engine)
    deliver_pending(engine)
    t = owner_text(row(engine, oid))
    assert t.startswith("Handed to the mail server at ") and "Delivery to the inbox is not confirmed." in t
    assert "delivered" not in t.lower().replace("delivery", "")


# --- the settings context --------------------------------------------------------------------------------------------

def test_the_connector_reads_live_settings_while_the_approved_sender_stays(engine, smtp, clock):
    oid = queue(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE smtpx_settings SET host = 'mail-2.example'")
    deliver_pending(engine)
    assert smtp.settings_seen[-1]["host"] == "mail-2.example"
    assert row(engine, oid)["from_address"] == "books@acme.example"


def test_the_settings_context_refuses_a_foreign_table_and_a_read_after_the_mark(engine, smtp, clock, monkeypatch):
    seen = {}

    def connector(settings, msg, mark_data):
        with pytest.raises(ReadRefused, match="is not a table of plugin smtpx"):
            settings.one("company")
        mark_data()
        with pytest.raises(ReadRefused, match="the settings context is closed"):
            settings.one("smtpx_settings")
        seen["ok"] = True
        return SendOutcome("after_data", "accepted", 250, "ok", None)

    smtp.send_message = connector
    replace_filler(monkeypatch, "send_message", connector)
    queue(engine)
    deliver_pending(engine)
    assert seen == {"ok": True}


def test_reading_settings_holds_no_outbox_lock(engine, smtp, clock, monkeypatch):
    oid = queue(engine)
    url = engine.url.render_as_string(hide_password=False)

    def connector(settings, msg, mark_data):
        other = make_engine(url)
        with other.begin() as conn:  # a second worker can lock the row while settings are read
            conn.exec_driver_sql("SET LOCAL lock_timeout = '2s'")
            conn.execute(select(m.outbox).where(m.outbox.c.id == oid).with_for_update())
        other.dispose()
        mark_data()
        return SendOutcome("after_data", "accepted", 250, "ok", None)

    replace_filler(monkeypatch, "send_message", connector)
    deliver_pending(engine)
    assert row(engine, oid)["status"] == "accepted"


# --- crash points at core's hooks ------------------------------------------------------------------------------------

class Crash(Exception):
    pass


@pytest.mark.parametrize("point,status,sent", [
    ("after_claim", "accepted", 1), ("after_data_mark", "unknown", 0), ("after_answer", "unknown", 1),
    ("after_result", "accepted", 1)])
def test_a_crash_at_each_point_sends_at_most_once(engine, smtp, clock, monkeypatch, point, status, sent):
    oid = queue(engine)

    def fault(at):
        if at == point:
            raise Crash(at)

    monkeypatch.setattr(outbox, "fault", fault)
    with pytest.raises(Crash):
        deliver_pending(engine)
    monkeypatch.setattr(outbox, "fault", lambda at: None)
    clock.advance(minutes=6)
    deliver_pending(engine)  # recovery first, then a run
    deliver_pending(engine)
    assert row(engine, oid)["status"] == status and len(smtp.bodies) == sent


# --- sending again, the sender, the folder adapter, the switch -------------------------------------------------------

def test_send_again_requeues_an_unknown_message_with_its_message_id(engine, smtp, clock):
    oid = queue(engine)
    a = outbox.claim(engine, oid)
    outbox.mark_data(engine, oid, a)
    clock.advance(minutes=6)
    outbox.recover(engine)
    before = row(engine, oid)["message_id"]
    with engine.begin() as conn:
        send_again(company_ctx(conn), oid)
    r = row(engine, oid)
    assert r["status"] == "queued" and r["message_id"] == before and r["current_attempt_id"] is None
    deliver_pending(engine)
    assert len(smtp.bodies) == 1 and len(attempts(engine, oid)) == 2


def test_send_again_refuses_a_message_that_was_accepted(engine, smtp, clock):
    oid = queue(engine)
    deliver_pending(engine)
    with pytest.raises(Refusal):
        with engine.begin() as conn:
            send_again(company_ctx(conn), oid)


def test_the_sender_comes_from_core_and_without_a_filler_only_emailing_is_refused(engine):
    with engine.connect() as conn:
        with pytest.raises(Refusal, match="No email account is set up for this company."):
            outbox.sender(company_ctx(conn))
    assert "sales" in plugins.REGISTRY.plugins  # sales still loads and invoices without an email plugin


def test_the_sender_snapshot_comes_from_the_filler(engine, smtp):
    with engine.connect() as conn:
        assert outbox.sender(company_ctx(conn)) == SENDER


def test_the_folder_connector_sends_only_in_demo_or_eval_books(engine, clock):
    with engine.begin() as conn:
        conn.execute(owner.delete().where(owner.c.mark == DEMO_MARK))
    oid = queue(engine)
    deliver_pending(engine)
    r = row(engine, oid)
    assert r["status"] == "queued" and "writes messages to a folder for the demo and the evals" in r["detail"]
    with engine.begin() as conn:
        conn.execute(owner.insert().values(mark=EVAL_MARK))
    deliver_pending(engine)
    r = row(engine, oid)
    assert r["status"] == "accepted" and owner_text(r).startswith("Written to the mail folder at ")
    assert attempts(engine, oid)[0]["data_at"] is not None



def test_a_crash_after_the_folder_result_still_says_written_to_the_folder(engine, clock, monkeypatch):
    """The folder adapter's marker commits with the accepted result, in one transaction: a crash just after the result
    never leaves an accepted folder message reading as handed to a mail server."""
    real = outbox.record_result

    def then_crash(*a, **k):
        real(*a, **k)
        raise SystemExit("crash after the result committed")

    monkeypatch.setattr(outbox, "record_result", then_crash)
    oid = queue(engine)
    with pytest.raises(SystemExit):
        deliver_pending(engine)
    r = row(engine, oid)
    assert r["status"] == "accepted" and owner_text(r).startswith("Written to the mail folder at "), owner_text(r)
    assert attempts(engine, oid)[0]["text"]  # the file it was written to, on the attempt

def test_while_planned_nothing_is_sent(engine, as_released):
    oid = queue(engine)
    assert deliver_pending(engine) == [] and row(engine, oid)["status"] == "queued"
    with pytest.raises(Refusal):
        with engine.begin() as conn:
            send_again(company_ctx(conn), oid)


def test_the_steps_run_as_the_runtime_role(engine, runtime_login, smtp, clock):
    rt = make_engine(runtime_login)
    with rt.begin() as conn:
        oid = enqueue(company_ctx(conn), "deliver_message", "ar@acme.example", "Hi", "Body", None, sender=SENDER)
    a = outbox.claim(rt, oid)
    assert outbox.mark_data(rt, oid, a) is True
    outbox.record_result(rt, oid, a, SendOutcome("after_data", "accepted", 250, "ok", None))
    with pytest.raises(DBAPIError):  # lease_until is written once, at insert
        with rt.begin() as conn:
            conn.execute(m.outbox_attempt.update().where(m.outbox_attempt.c.id == a).values(lease_until=None))
    rt.dispose()
    assert row(engine, oid)["status"] == "accepted"
