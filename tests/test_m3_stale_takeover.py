"""A stale takeover must never overwrite a later request's state."""
from datetime import datetime, timedelta, timezone

import pytest

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import principals as pr


class Clock:
    def __init__(self):
        self.at = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.at += timedelta(milliseconds=1)
        return self.at

    def lapse(self):
        self.at += store.LEASE + timedelta(seconds=1)


@pytest.mark.parametrize("boundary", ["retired", "intent"])
@pytest.mark.parametrize("kind", ["card", "question"])
def test_a_paused_turn_cannot_change_a_later_holders_request(engine, monkeypatch, boundary, kind):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    clock = Clock()
    monkeypatch.setattr(store, "_now", clock)
    a = Assistant(engine, ScriptedModel([Reply("", [call(
        "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])])]))
    cid = "m3-stale-takeover"
    assert any(e["type"] == "card" for e in a.turn(cid, "Invoice Acme Corporation for 5 copper wire"))
    abandoned = store.claim(cid, "stopped before activation",
                            principal_id=claimant["id"], principal_name=claimant["name"])
    req = store.open_request(cid)
    assert store.retoken(abandoned, req["id"])
    clock.lapse()
    if boundary == "intent":
        current = store.claim(cid, "assigned before activation",
                              principal_id=claimant["id"], principal_name=claimant["name"])
        assert current and store.assign(current, req["id"])
        clock.lapse()
    original = a._reconcile if boundary == "retired" else a._close_intent
    captured = {}

    def pause_then_lose_claim(*args, **kwargs):
        assert not captured
        live_req = store.open_request(cid)
        clock.lapse()
        later = store.claim(cid, "newer holder", principal_id=claimant["id"], principal_name=claimant["name"])
        assert later and store.assign(later, live_req["id"])
        question = {"id": "q7", "field": "create_invoice#0:customer", "choices": []} if kind == "question" else None
        card = "synthetic-newer-card" if kind == "card" else None
        assert store.finalise(later, live_req["id"], [{"type": "notice", "text": "Newer holder completed"}],
                              pending_question=question, pending_card=card, state="open", why=None,
                              serial_now=7, bindings_now={"synthetic": "newer binding"}, intent_state="proposed")
        captured["request"] = store.get_request(req["id"])
        if boundary == "intent":
            latest = store.claim(cid, "later live intent", principal_id=claimant["id"], principal_name=claimant["name"])
            assert latest and store.assign(latest, req["id"])
        captured["intent"] = store.intent(cid)
        captured["events"] = store.events(req["id"])
        captured["bindings"] = store.bindings(req["id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(a, "_reconcile" if boundary == "retired" else "_close_intent", pause_then_lose_claim)
    result = a.turn(cid, "resume old takeover")
    assert result and all(e["type"] != "card" for e in result), result
    assert captured, "the intended suspension boundary was not reached"
    assert store.get_request(req["id"]) == captured["request"]
    assert store.intent(cid) == captured["intent"]
    assert store.events(req["id"]) == captured["events"]
    assert store.bindings(req["id"]) == captured["bindings"]

def test_crash_after_closing_retired_intent_does_not_strand_the_pending_card(engine, monkeypatch):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    clock = Clock()
    monkeypatch.setattr(store, "_now", clock)
    a = Assistant(engine, ScriptedModel([Reply("", [call(
        "create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])])]))
    cid = "m3-crashed-takeover"
    card = next(e for e in a.turn(cid, "Invoice Acme Corporation for 5 copper wire") if e["type"] == "card")
    abandoned = store.claim(cid, "stopped before activation",
                            principal_id=claimant["id"], principal_name=claimant["name"])
    req = store.open_request(cid)
    assert store.retoken(abandoned, req["id"])
    clock.lapse()
    stopped = store.claim(cid, "assigned before activation",
                          principal_id=claimant["id"], principal_name=claimant["name"])
    assert stopped and store.assign(stopped, req["id"])
    clock.lapse()
    original = a._close_intent
    reached = []

    def close_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        reached.append(True)
        assert store.outcome_applied(card["approval_id"], "superseded")
        raise RuntimeError("synthetic crash after retired intent commit")

    monkeypatch.setattr(a, "_close_intent", close_then_crash)
    with pytest.raises(RuntimeError, match="after retired intent commit"):
        a.turn(cid, "resume old takeover")
    assert reached
    monkeypatch.setattr(a, "_close_intent", original)
    clock.lapse()
    a.model.replies.append(Reply("Nothing more to change.", []))
    events = a.turn(cid, "thanks")
    assert not any(e.get("code") == "reconciliation_incomplete" for e in events), events
