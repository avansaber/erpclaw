"""Reconciliation crash, legacy and concurrency checks without message IDs."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import principals as pr
from erpclaw_core import approvals, models as m
from erpclaw_core.common import company_ctx, public_data
from erpclaw_core.masters import create_party


class Clock:
    def __init__(self):
        self.at = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.at += timedelta(milliseconds=1)
        return self.at

    def lapse(self):
        self.at += store.LEASE + timedelta(seconds=1)


def setup_question(engine, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(store, "_now", clock)
    def inv(customer):
        return call("create_invoice", customer=customer, items=[{"item": "copper wire", "quantity": 2}])
    a = Assistant(engine, ScriptedModel([Reply("", [inv("Acme Corporation")]),
                                         Reply("", [inv("Acme Corporation North")])]))
    card = a.turn("reconcile-atomic", "Invoice Acme Corporation for 2 copper wire")[-1]
    assert card["type"] == "card"
    with engine.begin() as conn:
        conn.execute(m.party.update().where(m.party.c.name == "Acme Corporation").values(name="Acme Corporation North"))
        create_party(company_ctx(conn), "customer", "Acme Corporation South")
    assert approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=_ownership(engine)).status == "superseded"
    return a, card, clock


def test_reconcile_crash_rolls_back_marker_and_next_turn_receives_question(engine, monkeypatch):
    a, card, clock = setup_question(engine, monkeypatch)
    conn = sqlite3.connect(store.path())
    conn.execute("CREATE TRIGGER fail_question BEFORE UPDATE OF pending_question ON request "
                 "WHEN NEW.pending_question IS NOT NULL BEGIN SELECT RAISE(ABORT, 'injected'); END")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        a.turn("reconcile-atomic", "Acme Corporation North")
    conn = sqlite3.connect(store.path())
    conn.execute("DROP TRIGGER fail_question")
    conn.commit()
    conn.close()
    assert not store.outcome_applied(card["approval_id"], "superseded")
    clock.lapse()
    seen = len(a.model.seen)
    events = a.turn("reconcile-atomic", "Acme Corporation North")
    assert [e["type"] for e in events] == ["notice", "ask"], events
    assert len(a.model.seen) == seen
    assert store.open_request("reconcile-atomic")["pending_question"] is not None


def test_existing_partial_marker_fails_closed(engine, monkeypatch):
    a, card, _ = setup_question(engine, monkeypatch)
    req = store.open_request("reconcile-atomic")
    assert store.apply_outcome(card["approval_id"], "superseded", req["id"], "question")
    seen = len(a.model.seen)
    events = a.turn("reconcile-atomic", "Acme Corporation North")
    assert [(e["type"], e.get("code")) for e in events] == [("refused", "reconciliation_incomplete")], events
    assert len(a.model.seen) == seen


@pytest.mark.parametrize("action", ["question", "replaced", "ended", "restored"])
def test_concurrent_reconciliation_records_the_outcome_once(engine, action):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    c = store.claim("parallel-reconcile", "first", principal_id=claimant["id"], principal_name=claimant["name"])
    req = store.new_request(c)
    store.set_pending(req["id"], card="old")
    event = {"type": "notice", "text": "outcome"}
    kw = {"events": [event]}
    if action == "question":
        kw.update(question={"id": "q1"}, serial=1, previous_serial=0)
    elif action == "replaced":
        kw.update(card="fresh")
    else:
        kw.update(end_reason=action)
    barrier = Barrier(2)
    def run():
        barrier.wait(timeout=10)
        return store.reconcile_outcome(c, "old", "missing" if action == "restored" else "superseded",
                                       req["id"], action, **kw)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ["applied", "stale"]
    assert store.events(req["id"]) == [event]


def test_crash_after_set_aside_preserves_private_question_choices(engine, monkeypatch):
    with engine.connect() as conn:
        claimant = pr.owner(conn)
    from erpclaw_assistant.resolve import Session

    clock = Clock()
    monkeypatch.setattr(store, "_now", clock)
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice])]))
    card = a.turn("private-refs", "Invoice Acme Corporation for 2 copper wire")[-1]
    assert card["type"] == "card"
    req = store.open_request("private-refs")
    c = store.claim("private-refs", "choose a warehouse", principal_id=claimant["id"], principal_name=claimant["name"])
    session = Session(store.serial("private-refs"))
    rf = session.ask("create_invoice#0:warehouse", "Which warehouse?", "warehouse_ambiguous",
                     ["Main (5 on hand)", "Back (2 on hand)"], ["Main", "Back"])
    ask = {"type": "ask", "text": rf.message, "code": rf.code, "options": rf.options,
           "data": public_data(rf.data)}
    question = dict(session.pending, event=ask)
    real = store.finalise

    def crash(*args, **kwargs):
        raise RuntimeError("crash after card set aside")

    monkeypatch.setattr(store, "finalise", crash)
    with pytest.raises(RuntimeError, match="card set aside"):
        a._finish(c, req, [ask], session, question)
    monkeypatch.setattr(store, "finalise", real)
    clock.lapse()
    events = a.turn("private-refs", "please continue")
    asked = next(e for e in events if e["type"] == "ask")
    assert all("ref" not in choice for choice in asked["data"]["choices"])
    pending = store.open_request("private-refs")["pending_question"]
    assert [choice["ref"] for choice in pending["choices"]] == ["Main", "Back"]
    outcome = approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=_ownership(engine))
    assert [choice["ref"] for choice in outcome.data["data"]["choices"]] == ["Main", "Back"]

    from erpclaw_api.app import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(engine, ScriptedModel([]), embedded_owner=True))
    replies = [client.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]})
               for _ in range(2)]
    assert all(r.status_code == 200 for r in replies)
    first, second = (r.json()["events"] for r in replies)
    assert first == second
    assert all("ref" not in ch for ev in first for ch in ev.get("data", {}).get("choices", []))
    assert all("ref" not in ch for ev in first for ch in ev.get("choices", []))


def _ownership(engine):
    from erpclaw_assistant import store
    from erpclaw_core import principals

    with engine.connect() as conn:
        owner = principals.owner(conn)["id"]
    return lambda cid: store.conversation_owner(cid) == owner
