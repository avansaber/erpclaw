"""Pure callback state / real _send orchestration with recording boundaries only."""
import threading
from types import SimpleNamespace

import pytest

from erpclaw_core import outbox as o


class Crash(Exception): pass
class Halt(BaseException): pass
class CloseError(Exception): pass
class Wrapped(Exception): pass


def answer(kind="accepted", marked=True):
    return o.SendOutcome("after_data" if marked else "before_data", kind, None, "")


@pytest.fixture
def harness(monkeypatch):
    h = SimpleNamespace(events=[], results=[], saved=[], close_count=0, close_errors={},
                        close_hook=None, mark_hook=None, fault_hook=None, mark_result=True,
                        engine=object(), aid="attempt", worker_errors=[])
    h.row = dict(id="message", company_id="company", message_id="<stable-message>",
                 recipient="owner@example.invalid", from_address="sender@example.invalid",
                 from_name="Sender", reply_to=None, subject="Invoice", body="Body")
    class Settings:
        def close(self):
            h.close_count += 1
            h.events.append(("close", h.close_count))
            if h.close_hook: h.close_hook()
            if h.close_count in h.close_errors: raise h.close_errors[h.close_count]
    h.settings = Settings()
    def readctx(engine, company, plugin):
        assert engine is h.engine and company == "company" and plugin == "email"
        h.events.append("settings")
        return h.settings
    def claim(engine, oid):
        assert engine is h.engine and oid == "message"
        h.events.append("claim")
        return h.aid
    def attachments(engine, row):
        h.events.append("attachments")
        return ()
    def mark(engine, oid, aid):
        assert engine is h.engine and (oid, aid) == ("message", "attempt")
        h.events.append("mark")
        if h.mark_hook: h.mark_hook()
        if isinstance(h.mark_result, BaseException): raise h.mark_result
        return h.mark_result
    def fault(point):
        h.events.append(point)
        if h.fault_hook: h.fault_hook(point)
    def record(engine, oid, aid, outcome, **kwargs):
        assert engine is h.engine and (oid, aid) == ("message", "attempt") and not kwargs
        h.events.append("result")
        h.results.append(outcome)
    monkeypatch.setattr(o, "ReadCtx", readctx)
    monkeypatch.setattr(o, "claim", claim)
    monkeypatch.setattr(o, "_read_attachments", attachments)
    monkeypatch.setattr(o, "mark_data", mark)
    monkeypatch.setattr(o, "fault", fault)
    monkeypatch.setattr(o, "record_result", record)
    def send(connector):
        def wrapped(settings, message, callback):
            assert settings is h.settings
            assert message.id == "message" and message.message_id == "<stable-message>"
            h.saved.append(callback)
            return connector(settings, message, callback)
        return o._send(h.engine, h.row, "email", wrapped)
    h.send = send
    return h


def assert_revoked(h):
    events = list(h.events)
    assert h.saved and h.saved[0]() is False
    assert h.events == events


def test_mark_once_then_true_outcome_preserves_order(harness):
    h = harness
    def connector(settings, msg, mark):
        assert mark() is True
        assert mark() is False
        h.events.append("DATA")
        return answer()
    h.send(connector)
    assert h.events == ["claim", "after_claim", "attachments", "settings", ("close", 1),
                        "mark", "after_data_mark", "DATA", ("close", 2), "after_answer", "result", "after_result"]
    assert h.results == [answer()]
    assert_revoked(h)


@pytest.mark.parametrize("failure", [False, ValueError("synthetic private detail"), "close"])
def test_failed_first_consumes_allowance_without_second_effect(harness, failure):
    h = harness
    if failure == "close": h.close_errors[1] = CloseError("first close")
    else: h.mark_result = failure
    def connector(settings, msg, mark):
        if failure == "close":
            with pytest.raises(CloseError): mark()
        else: assert mark() is False
        assert mark() is False
        return answer("lost_ownership", False)
    h.send(connector)
    assert h.events.count("mark") == (0 if failure == "close" else 1)
    assert "after_data_mark" not in h.events
    assert h.close_count == 2
    assert h.results == [answer("lost_ownership", False)]
    assert_revoked(h)


@pytest.mark.parametrize("point", ["close", "mark", "fault"])
def test_reentry_is_false_without_additional_effect_or_deadlock(harness, point):
    h = harness; reentered = []
    def reenter(): reentered.append(h.saved[0]())
    if point == "close": h.close_hook = reenter
    elif point == "mark": h.mark_hook = reenter
    else: h.fault_hook = lambda p: reenter() if p == "after_data_mark" else None
    h.send(lambda settings, msg, mark: answer() if mark() else answer("lost_ownership", False))
    assert reentered and all(x is False for x in reentered)
    assert h.events.count("mark") == 1 and h.events.count("after_data_mark") == 1
    assert_revoked(h)


@pytest.mark.parametrize("marked", [False, True])
def test_ordinary_connector_exception_redacted_and_classified_after_cleanup(harness, marked):
    h = harness
    def connector(settings, msg, mark):
        if marked: assert mark()
        raise ValueError("synthetic private detail never recorded")
    h.send(connector)
    assert h.results == [o.SendOutcome("after_data" if marked else "before_data",
                                       "no_answer" if marked else "temporary", None, "ValueError")]
    assert h.events.index(("close", h.close_count)) < h.events.index("result")
    assert_revoked(h)


@pytest.mark.parametrize("marked", [False, True])
def test_invalid_outcome_uses_completed_mark_state(harness, marked):
    h = harness
    def connector(settings, msg, mark):
        if marked: assert mark()
        return object()
    h.send(connector)
    assert h.results == [o.SendOutcome("after_data" if marked else "before_data",
                                       "no_answer" if marked else "temporary", None,
                                       "the connector returned no outcome")]
    assert_revoked(h)


@pytest.mark.parametrize("failure", [Crash("deliberate"), Halt("deliberate"), KeyboardInterrupt()])
def test_controlled_fault_propagates_after_cleanup_without_result(harness, failure):
    h = harness
    def fault(point):
        if point == "after_data_mark": raise failure
    h.fault_hook = fault
    with pytest.raises(type(failure)) as exc:
        h.send(lambda settings, msg, mark: answer() if mark() else answer("lost_ownership", False))
    assert exc.value is failure and not h.results and h.close_count == 2
    assert "after_answer" not in h.events
    assert_revoked(h)


def test_wrapped_controlled_fault_retains_chain(harness):
    h = harness; original = Crash("deliberate")
    def fault(point):
        if point == "after_data_mark": raise original
    h.fault_hook = fault
    def connector(settings, msg, mark):
        try: mark()
        except Crash as exc: raise Wrapped("wrapper") from exc
    with pytest.raises(Wrapped) as exc: h.send(connector)
    assert exc.value.__cause__ is original and not h.results
    assert_revoked(h)


@pytest.mark.parametrize("failure", [Crash("swallowed"), Halt("swallowed")])
def test_swallowed_fault_normal_return_preserves_trusted_connector_limitation(harness, failure):
    h = harness
    def fault(point):
        if point == "after_data_mark": raise failure
    h.fault_hook = fault
    def connector(settings, msg, mark):
        try: mark()
        except BaseException: pass
        return answer()
    h.send(connector)
    assert h.results == [answer()]
    assert_revoked(h)


@pytest.mark.parametrize("marked", [False, True])
def test_original_baseexception_after_successful_cleanup_is_preserved(harness, marked):
    h = harness; failure = Halt("stop")
    def connector(settings, msg, mark):
        if marked: mark()
        raise failure
    with pytest.raises(Halt) as exc: h.send(connector)
    assert exc.value is failure and not h.results
    assert h.close_count == (2 if marked else 1)
    assert_revoked(h)


def test_repeated_failed_close_retried_and_final_failure_wins(harness):
    h = harness; first = CloseError("first"); final = CloseError("final")
    h.close_errors = {1: first, 2: final}
    with pytest.raises(CloseError) as exc: h.send(lambda settings, msg, mark: mark())
    assert exc.value is final and exc.value.__context__ is first
    assert h.close_count == 2 and "mark" not in h.events and not h.results
    assert_revoked(h)


@pytest.mark.parametrize("original", [ValueError("connector"), Halt("connector")])
def test_final_close_exception_overrides_pending_connector(harness, original):
    h = harness; final = CloseError("final");h.close_errors[1] = final
    def connector(settings, msg, mark): raise original
    with pytest.raises(CloseError) as exc: h.send(connector)
    assert exc.value is final and exc.value.__context__ is original and not h.results
    assert_revoked(h)


def run_inflight_case(h, monkeypatch, exit_kind, interruptions=(), final_error=None):
    entered = threading.Event(); release = threading.Event(); waiting = threading.Event()
    callbacks = []; send_errors = []; interrupt_queue = list(interruptions)
    class ObservedCondition(threading.Condition):
        def wait(self, timeout=None):
            waiting.set()
            if interrupt_queue: raise interrupt_queue.pop(0)
            return super().wait(timeout)
    monkeypatch.setattr(o, "Condition", ObservedCondition)
    if final_error: h.close_errors[2] = final_error
    def mark_hook():
        entered.set()
        assert release.wait(3), "test did not release admitted callback"
    h.mark_hook = mark_hook
    def connector(settings, msg, mark):
        def invoke():
            try: callbacks.append(mark())
            except BaseException as exc: h.worker_errors.append(exc)
        worker = threading.Thread(target=invoke)
        threads.append(worker);worker.start()
        assert entered.wait(3)
        if exit_kind == "exception": raise ValueError("private text")
        if exit_kind == "halt": raise Halt("pending connector")
        return answer() if exit_kind == "accepted" else None
    def invoke_send():
        try: h.send(connector)
        except BaseException as exc: send_errors.append(exc)
    threads = []
    sender = threading.Thread(target=invoke_send);threads.append(sender);sender.start()
    try:
        assert waiting.wait(3), "send never entered drain"
        assert h.close_count == 1 and not h.results
        assert h.saved[0]() is False  # closing beats a later admission
        assert h.events.count("mark") == 1
    finally:
        release.set()
        for thread in threads: thread.join(3)
    assert all(not thread.is_alive() for thread in threads)
    assert not h.worker_errors and callbacks == [True]
    assert h.close_count == 2
    assert_revoked(h)
    return send_errors


@pytest.mark.parametrize("exit_kind", ["accepted", "exception", "invalid"])
def test_result_waits_for_admitted_mark_and_uses_final_snapshot(harness, monkeypatch, exit_kind):
    h = harness;errors = run_inflight_case(h, monkeypatch, exit_kind)
    assert errors == []
    assert h.results[0].kind == ("accepted" if exit_kind == "accepted" else "no_answer")
    assert h.results[0].stage == "after_data"
    assert h.events.index("after_data_mark") < h.events.index(("close", 2)) < h.events.index("result")
    if exit_kind == "exception": assert h.results[0].text == "ValueError"


@pytest.mark.parametrize("first", [RuntimeError("first interruption"), Halt("first interruption")])
def test_interrupted_drain_keeps_waiting_and_retains_first_interruption(harness, monkeypatch, first):
    h = harness; second = RuntimeError("second interruption")
    errors = run_inflight_case(h, monkeypatch, "accepted", (first, second))
    assert errors == [first] and not h.results and "after_answer" not in h.events
    assert h.events.index("after_data_mark") < h.events.index(("close", 2))


def test_final_close_wins_drain_interruption_and_original_baseexception(harness, monkeypatch):
    h = harness; interrupted = Halt("drain"); final = CloseError("final")
    errors = run_inflight_case(h, monkeypatch, "halt", (interrupted,), final)
    assert errors == [final] and final.__cause__ is interrupted
    assert isinstance(interrupted.__context__, Halt)
    assert str(interrupted.__context__) == "pending connector"
    assert not h.results


@pytest.mark.parametrize("outcome", [answer("accepted"), answer("temporary"), answer("permanent"),
                                      answer("no_answer"), answer("lost_ownership", False)])
def test_existing_typed_outcomes_are_forwarded_without_status_rewrite(harness, outcome):
    h = harness
    h.send(lambda settings, msg, mark: outcome)
    assert h.results == [outcome]
    assert_revoked(h)


@pytest.mark.parametrize("point", ["after_answer", "after_result"])
def test_later_faults_keep_existing_result_order_and_revoke(harness, point):
    h = harness; failure = Crash(point)
    def fault(actual):
        if actual == point: raise failure
    h.fault_hook = fault
    with pytest.raises(Crash) as exc: h.send(lambda settings, msg, mark: answer())
    assert exc.value is failure
    assert len(h.results) == (1 if point == "after_result" else 0)
    assert_revoked(h)


def test_after_claim_failure_happens_before_settings_or_handle(harness):
    h = harness
    def fault(point):
        if point == "after_claim": raise Crash("claim")
    h.fault_hook = fault
    with pytest.raises(Crash): h.send(lambda *args: pytest.fail("connector must not run"))
    assert h.events == ["claim", "after_claim"] and not h.saved and not h.results


def test_attachment_failure_stays_before_settings_and_records_unavailable(harness, monkeypatch):
    h = harness
    def unavailable(*args): raise o.AttachmentUnavailable("synthetic")
    monkeypatch.setattr(o, "_read_attachments", unavailable)
    h.send(lambda *args: pytest.fail("connector must not run"))
    assert h.events == ["claim", "after_claim", "result"]
    assert h.results == [o.UNAVAILABLE] and not h.saved


def test_unclaimable_message_never_constructs_handle(harness):
    h = harness;h.aid = None
    h.send(lambda *args: pytest.fail("connector must not run"))
    assert h.events == ["claim"] and not h.saved and not h.results
