"""One turn per conversation, and the fence: a second message while a turn is working is refused as busy, another
conversation is never blocked, and a turn whose lease lapsed while it was paused never makes or keeps a card after a
newer message took its conversation over."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core import models as m


def inv(customer, quantity, item="copper wire"):
    return call("create_invoice", customer=customer, items=[{"item": item, "quantity": quantity}])


class Paused:
    """A model that waits, at the call the test chooses, until the test lets it go on."""

    def __init__(self, replies, pause_at=0):
        self.inner, self.calls, self.pause_at = ScriptedModel(list(replies)), 0, pause_at
        self.entered, self.go = threading.Event(), threading.Event()

    def complete(self, messages, tools):
        k, self.calls = self.calls, self.calls + 1
        if k == self.pause_at:
            self.entered.set()
            assert self.go.wait(60), "the test never let the paused model go on"
        return self.inner.complete(messages, tools)


def in_thread(fn, *args):
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("events", fn(*args)))
    th.start()
    return th, out


def cards(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(m.approval)).scalar_one()


def version(conversation_id) -> int:
    import sqlite3

    conn = sqlite3.connect(store.path())
    try:
        return conn.execute("SELECT version FROM conversation WHERE id = ?", (conversation_id,)).fetchone()[0]
    finally:
        conn.close()


def test_two_messages_at_once_one_turn_runs_and_the_other_is_busy(engine):
    model = Paused([Reply("", [inv("Acme Corporation", 5)])])
    a = Assistant(engine, model)
    th, out = in_thread(a.turn, "c1", "Invoice Acme Corporation for 5 copper wire")
    assert model.entered.wait(30)
    ev = Assistant(engine, ScriptedModel([])).turn("c1", "and Hooli too")
    assert [(e["type"], e.get("code")) for e in ev] == [("refused", "busy")], ev
    model.go.set()
    th.join(60)
    assert [e["type"] for e in out["events"]] == ["card"], out
    assert version("c1") == 1  # the busy message changed nothing; the one turn advanced it once


def test_another_conversation_completes_while_the_first_s_model_call_is_paused(engine):
    model = Paused([Reply("Hello.")])
    th, out = in_thread(Assistant(engine, model).turn, "c1", "hello there")
    assert model.entered.wait(30)
    ev = Assistant(engine, ScriptedModel([Reply("", [inv("Acme Corporation", 5)])])).turn(
        "c2", "Invoice Acme Corporation for 5 copper wire")
    assert [e["type"] for e in ev] == ["card"], ev  # no store-wide lock held through the paused call
    model.go.set()
    th.join(60)


def test_a_turn_paused_past_its_lease_stops_when_it_wakes_and_makes_no_card(engine, monkeypatch):
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=1))
    model = Paused([Reply("", [inv("Acme Corporation", 5)])], pause_at=0)
    th, out = in_thread(Assistant(engine, model).turn, "c1", "Invoice Acme Corporation for 5 copper wire")
    assert model.entered.wait(30)
    threading.Event().wait(1.5)  # the lease lapses while the model is paused
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=300))
    newer = Assistant(engine, ScriptedModel([Reply("Hello.")])).turn("c1", "hello?")
    assert [e["type"] for e in newer] == ["answer"], newer  # the newer message took the conversation over
    before = cards(engine)
    model.go.set()
    th.join(60)
    assert [(e["type"], e.get("code")) for e in out["events"]] == [("refused", "busy")], out
    assert cards(engine) == before  # the stalled turn made nothing


def test_a_stalled_turn_whose_renewal_is_skipped_is_refused_by_the_fence(engine, monkeypatch):
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=1))
    model = Paused([Reply("", [inv("Acme Corporation", 5)])], pause_at=0)
    th, out = in_thread(Assistant(engine, model).turn, "c1", "Invoice Acme Corporation for 5 copper wire")
    assert model.entered.wait(30)
    threading.Event().wait(1.5)
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=300))
    Assistant(engine, ScriptedModel([Reply("Hello.")])).turn("c1", "hello?")  # takes the conversation over
    monkeypatch.setattr(store, "renew", lambda c: True)  # a fault: the stalled turn never sees its lease is gone
    before = cards(engine)
    model.go.set()
    th.join(60)
    assert cards(engine) == before, out  # its propose is refused: its request's token is no longer live


def test_a_takeover_in_the_same_request_sets_the_stalled_worker_s_card_aside(engine, monkeypatch):
    first = Assistant(engine, ScriptedModel([Reply("", [inv("Acme Corporation", 5)])]))
    card = first.turn("c1", "Invoice Acme Corporation for 5 copper wire")[-1]
    assert card["type"] == "card"
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=1))
    model = Paused([Reply("", [inv("Acme Corporation", 3)])], pause_at=0)
    th, out = in_thread(Assistant(engine, model).turn, "c1", "actually make it 3")
    assert model.entered.wait(30)
    threading.Event().wait(1.5)
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=300))
    ev = Assistant(engine, ScriptedModel([Reply("", [inv("Acme Corporation", 4)])])).turn("c1", "no, make it 4")
    assert ev[0]["type"] == "notice" and ev[0]["code"] == "card_set_aside", ev  # the reply says why
    assert approvals.approve(engine, card["approval_id"], card["digest"]).status == "superseded"
    model.go.set()
    th.join(60)
    with engine.connect() as conn:
        pending = conn.execute(select(func.count()).select_from(m.approval).where(
            m.approval.c.conversation_id == "c1", m.approval.c.status == "pending")).scalar_one()
    assert pending == 1  # exactly one pending card afterwards: the newer turn's


def test_a_takeover_that_keeps_the_request_open_still_fences_the_stalled_turn_out(engine, monkeypatch):
    """The newer turn ends with a question, so the request stays open: the stalled turn's token must still be dead,
    because a takeover gives the request a fresh one."""
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=1))
    model = Paused([Reply("", [inv("Acme Corporation", 5)])], pause_at=0)
    th, out = in_thread(Assistant(engine, model).turn, "c1", "Invoice Acme Corporation for 5 copper wire")
    assert model.entered.wait(30)
    threading.Event().wait(1.5)
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=300))
    asked = Assistant(engine, ScriptedModel([Reply("", [inv("Wayne", 5)])])).turn("c1", "or for Wayne")
    assert asked[-1]["type"] == "ask", asked  # the request stays open
    monkeypatch.setattr(store, "renew", lambda c: True)  # a fault: the stalled turn never sees its lease is gone
    before = cards(engine)
    model.go.set()
    th.join(60)
    assert cards(engine) == before, out  # no card under the stalled turn's generation


# ---------- the busy reply says what the store recorded ----------

def test_the_busy_reply_says_working_while_the_holder_renews_and_stopped_after(engine, monkeypatch):
    """Chosen from the holder's last recorded renewal, on an injected clock: within STALLED_AFTER the turn is still
    working; beyond it (and before the lease lapses) it stopped partway, and nothing was saved."""
    from datetime import datetime, timezone

    from erpclaw_assistant.resolve import _say

    t0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    clock = [t0]
    monkeypatch.setattr(store, "_now", lambda: clock[0])
    assert store.STALLED_AFTER < store.LEASE
    holder = store.claim("c1", "an earlier message", turn_id="another-turn")
    idle = Assistant(engine, ScriptedModel([]))

    def busy_text():
        [e] = idle.turn("c1", "anything?")
        assert (e["type"], e["code"]) == ("refused", "busy"), e
        return e["text"]

    clock[0] = t0 + store.STALLED_AFTER - timedelta(seconds=1)
    assert busy_text() == _say("busy")
    clock[0] = t0 + store.STALLED_AFTER + timedelta(seconds=1)
    assert busy_text() == _say("busy_stopped")
    assert store.renew(holder)  # the holder renews: it is working again, whatever its first claim's time
    clock[0] += timedelta(seconds=5)
    assert busy_text() == _say("busy")


def _status(engine, approval_id):
    with engine.connect() as conn:
        return conn.execute(select(m.approval.c.status).where(m.approval.c.id == approval_id)).scalar_one()


@pytest.mark.parametrize("faulty", [False, True])
def test_a_re_proposal_in_a_turn_taken_over_makes_no_card_and_leaves_the_old_one_as_the_takeover_did(
        engine, monkeypatch, faulty):
    first = next(e for e in Assistant(engine, ScriptedModel([Reply("", [inv("Acme Corporation", 5)])])).turn(
        "c1", "Invoice Acme Corporation for 5 copper wire") if e["type"] == "card")
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=1))
    model = Paused([Reply("Got it, Wayne instead of Acme.")], pause_at=0)  # the correction answered in text
    th, out = in_thread(Assistant(engine, model).turn, "c1", "no, Wayne, not Acme")
    assert model.entered.wait(30)
    threading.Event().wait(1.5)  # the lease lapses while the model is paused
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=300))
    Assistant(engine, ScriptedModel([Reply("Hello.")])).turn("c1", "hello?")  # takes the conversation over
    if faulty:  # the stalled turn never sees its lease is gone, so the fence at propose must stop it
        monkeypatch.setattr(store, "renew", lambda c: True)
    before, status = cards(engine), _status(engine, first["approval_id"])
    model.go.set()
    th.join(60)
    assert cards(engine) == before, out  # the stalled turn's re-proposal made nothing
    assert _status(engine, first["approval_id"]) == status, out  # and left the old card as the takeover did


class Lapses:
    """A model whose reply arrives after the turn's lease has lapsed: the store's clock moves past it meanwhile."""

    def __init__(self, monkeypatch, replies, lapse_at):
        self.inner, self.calls, self.lapse_at, self.monkeypatch = ScriptedModel(list(replies)), 0, lapse_at, monkeypatch

    def complete(self, messages, tools):
        k, self.calls = self.calls, self.calls + 1
        if k == self.lapse_at:
            real = store._now
            self.monkeypatch.setattr(store, "_now", lambda: real() + store.LEASE + timedelta(seconds=1))
        return self.inner.complete(messages, tools)


def _status(engine, approval_id):
    with engine.connect() as conn:
        return conn.execute(select(m.approval.c.status).where(m.approval.c.id == approval_id)).scalar_one()


def test_a_turn_whose_lease_lapsed_before_the_set_aside_leaves_the_card_as_it_was(engine, monkeypatch):
    model = Lapses(monkeypatch, [Reply("", [inv("Acme Corporation", 5)]), Reply("Okay, cancelled.")], lapse_at=1)
    a = Assistant(engine, model)
    [card] = a.turn("c1", "invoice Acme Corporation for 5 copper wire")
    ev = a.turn("c1", "cancel that")  # an unchanged re-proposal would set the card aside
    assert [e.get("code") for e in ev] == ["busy"], ev
    assert _status(engine, card["approval_id"]) == "pending"  # the books and the store still agree
    assert store.open_request("c1")["pending_card"] == card["approval_id"]
