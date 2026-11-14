"""Two turns on one conversation, run through every ordering of their steps (claim, fence, lease renewals, propose,
finalise), with the earlier turn killed at each of its steps, and the earlier turn's lease lapsing before the later
message: after every schedule, at most one card is pending, its token is the fence's live one, no card was made
under a stale generation, and every card a turn showed the owner is still approvable or its end was announced."""
from __future__ import annotations

import itertools
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from erpclaw_assistant import graph, store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core import models as m

# the points a turn that makes a card passes, in its own order (the lease is renewed before the model and before propose)
STEPS = ("claim", "activate", "renew", "renew", "propose", "finalise")


class Killed(BaseException):
    """The process running a turn died at this step: nothing after it runs, nothing is released."""


class Scheduler:
    """Lets exactly one turn run at a time, from one step to its next, in the order the schedule names."""

    def __init__(self, order, kill=None):
        self.order, self.kill = list(order), kill  # kill: (turn, step index) where that turn dies
        self.cond, self.turn = threading.Condition(), None
        self.passed = {"A": 0, "B": 0}
        self.done = set()

    def point(self, who):
        with self.cond:
            while not (self.turn == who):
                self.cond.wait(10)
            k = self.passed[who]
            self.passed[who] += 1
            if self.kill == (who, k):
                self.done.add(who)
                self._next()
                raise Killed()

    def finish(self, who):
        with self.cond:
            self.done.add(who)
            self._next()

    def step_done(self, who):
        with self.cond:
            self._next()

    def _next(self):
        while self.order:
            nxt = self.order.pop(0)
            if nxt not in self.done:
                self.turn = nxt
                self.cond.notify_all()
                return
        # the schedule ran out: let whoever is left finish in turn
        left = [w for w in ("A", "B") if w not in self.done]
        self.turn = left[0] if left else None
        self.cond.notify_all()

    def start(self):
        with self.cond:
            self._next()


def schedules():
    """Every interleaving of the two turns' steps; the later message (B) arrives after A's lease lapsed."""
    n = len(STEPS)
    for positions in itertools.combinations(range(2 * n), n):
        yield "".join("A" if i in positions else "B" for i in range(2 * n))


def run(engine, monkeypatch, order, kill):
    t0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    clock = [t0]
    monkeypatch.setattr(store, "_now", lambda: clock[0])
    sched = Scheduler(order, kill)
    local = threading.local()
    log = []  # (turn, step, what it returned), in the order the steps ran
    real = {"claim": store.claim, "activate": approvals._activate, "renew": store.renew,
            "propose": approvals.propose, "finalise": store.finalise}

    def hooked(name, fn):
        def wrapper(*a, **k):
            who = getattr(local, "who", None)
            if who is None:
                return fn(*a, **k)
            sched.point(who)
            try:
                if name == "claim" and who == "B":
                    clock[0] = t0 + store.LEASE + timedelta(seconds=1)  # A's lease lapsed before B's message
                result = fn(*a, **k)
                log.append((who, name, result))
                return result
            except Exception as e:
                log.append((who, name, e))
                raise
            finally:
                sched.step_done(who)
        return wrapper

    monkeypatch.setattr(store, "claim", hooked("claim", real["claim"]))
    monkeypatch.setattr(store, "renew", hooked("renew", real["renew"]))
    monkeypatch.setattr(store, "finalise", hooked("finalise", real["finalise"]))
    monkeypatch.setattr(approvals, "_activate", hooked("activate", real["activate"]))
    monkeypatch.setattr(approvals, "propose", hooked("propose", real["propose"]))
    out = {}

    def turn(who, said, reply):
        local.who = who
        try:
            out[who] = Assistant(engine, ScriptedModel([reply] * 3)).turn("c1", said)
        except Killed:
            out[who] = None
        finally:
            sched.finish(who)

    a = threading.Thread(target=turn, args=("A", "Invoice Acme Corporation for 5 copper wire",
                                            Reply("", [call("create_invoice", customer="Acme Corporation",
                                                            items=[{"item": "copper wire", "quantity": 5}])])))
    b = threading.Thread(target=turn, args=("B", "Invoice Hooli for 3 copper wire",
                                            Reply("", [call("create_invoice", customer="Hooli",
                                                            items=[{"item": "copper wire", "quantity": 3}])])))
    a.start()
    b.start()
    sched.start()
    a.join(60)
    b.join(60)
    assert not a.is_alive() and not b.is_alive(), (order, kill)
    # the owner's next message, once any lease has lapsed: an end not yet said is said here
    clock[0] = clock[0] + store.LEASE + timedelta(seconds=1)
    out["C"] = Assistant(engine, ScriptedModel([Reply("Hello.")] * 3)).turn("c1", "hello?")
    return out, log


def made(result) -> bool:
    return isinstance(result, approvals.Outcome) and result.status == "card"


def check(engine, out, log, order, kill):
    fenced = next((n for n, (who, step, r) in enumerate(log)
                   if who == "B" and step == "activate" and getattr(r, "status", None) == "active"), None)
    if fenced is not None:  # nothing proposed under the stale generation once the newer turn holds the fence
        late = [r for who, step, r in log[fenced + 1:] if who == "A" and step == "propose" and made(r)]
        assert not late, (order, kill, late)
    with engine.connect() as conn:
        fence = conn.execute(select(m.conversation_fence).where(
            m.conversation_fence.c.conversation_id == "c1")).mappings().first()
        cards = [dict(r) for r in conn.execute(select(m.approval).where(
            m.approval.c.conversation_id == "c1")).mappings()]
    pending = [c for c in cards if c["status"] == "pending"]
    assert len(pending) <= 1, (order, kill, pending)
    for c in pending:  # a pending card is always one the live fence admits
        assert fence and fence["live_token"] and c["request_card_token"] == fence["live_token"], (order, kill, c)
    shown = {e["approval_id"] for who in ("A", "B", "C") for e in (out.get(who) or []) if e["type"] == "card"}
    announced = any(e["type"] == "notice" for who in ("A", "B", "C") for e in (out.get(who) or []))
    for c in cards:
        if c["id"] in shown and c["status"] != "pending":
            assert announced or c["status"] == "consumed", (order, kill, c["status"])  # an end said, never silent


@pytest.mark.parametrize("kill", [None] + [("A", k) for k in range(len(STEPS) + 1)],
                         ids=lambda k: "no kill" if k is None else f"A killed at step {k[1]}")
def test_every_ordering_of_two_turns_keeps_the_fence(engine, monkeypatch, kill):
    for order in schedules():
        out, log = run(engine, monkeypatch, order, kill)
        assert sum(1 for who, _, _ in log if who == "A") <= len(STEPS), (order, log)  # the schedule covers each step
        check(engine, out, log, order, kill)
        with engine.begin() as conn:  # the next schedule starts from a conversation of its own
            conn.execute(m.approval.delete().where(m.approval.c.conversation_id == "c1"))
            conn.execute(m.conversation_fence.delete().where(m.conversation_fence.c.conversation_id == "c1"))
        store.path().unlink(missing_ok=True)
        store._ready.clear()
