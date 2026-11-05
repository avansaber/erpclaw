"""The approval row: the conversation fence, one pending card per conversation, the
stored decision, the request token and `replaces`. Each test fails when its rule is removed. PostgreSQL only."""
from __future__ import annotations

import itertools
import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from bookdiff import assert_changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import administration as admin
from erpclaw_core import approvals, ledger, models as m
from erpclaw_core import principals as pr
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import tables as t
from test_approvals_concurrency import a_session_waits, no_deadlock, run

INVOICE = {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 5}]}
PAYMENT = {"customer": "Pied Piper", "amount": "100", "oldest_first": True, "method": "check"}


def company(engine) -> str:
    with engine.connect() as conn:
        return company_ctx(conn).company_id


def propose(engine, conv=None, token=None, args=INVOICE, contract="create_invoice", replaces=None, **kw):
    return approvals.propose(engine, REGISTRY.contracts[contract], args, conv, request_card_token=token,
                             replaces=replaces, **kw)


def card(engine, aid) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(select(m.approval).where(m.approval.c.id == aid)).mappings().one())


def status(engine, aid) -> str:
    return card(engine, aid)["status"]


def fence(engine, conv) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(select(m.conversation_fence).where(m.conversation_fence.c.conversation_id == conv)
                           ).mappings().first()
    return dict(row) if row else None


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def decision(engine, aid) -> dict:
    return json.loads(card(engine, aid)["decision_json"])


# --- the fence --------------------------------------------------------------------------------------------------

def test_a_higher_generation_activates_and_sets_aside_cards_of_other_tokens(engine):
    cid = company(engine)
    assert approvals.activate(engine, "c1", cid, 1, "T1").status == "active"
    old = propose(engine, "c1", "T1")
    act = approvals.activate(engine, "c1", cid, 2, "T2")
    assert (act.status, act.generation, act.set_aside) == ("active", 2, [old.approval_id])
    assert status(engine, old.approval_id) == "superseded"
    assert decision(engine, old.approval_id) == {"status": "superseded", "text": approvals.ENDED}
    assert (fence(engine, "c1")["generation"], fence(engine, "c1")["live_token"]) == (2, "T2")


def test_a_continuing_request_keeps_its_token_so_its_card_stays(engine):
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 1, "T1")
    mine = propose(engine, "c1", "T1")
    assert approvals.activate(engine, "c1", cid, 2, "T1").set_aside == []
    assert status(engine, mine.approval_id) == "pending"


@pytest.mark.parametrize("generation", [2, 1])
def test_an_equal_or_lower_generation_changes_nothing_and_returns_the_fences(engine, generation):
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 2, "T2")
    live = propose(engine, "c1", "T2")
    before = snapshot(engine)
    act = approvals.activate(engine, "c1", cid, generation, "T9")
    assert (act.status, act.generation, act.set_aside) == ("stale", 2, [])
    assert snapshot(engine) == before and status(engine, live.approval_id) == "pending"


def test_a_failed_activation_on_a_new_conversation_writes_nothing(engine):
    before = snapshot(engine)
    assert approvals.activate(engine, "c-new", company(engine), 0, "T0").status == "stale"
    assert snapshot(engine) == before  # not even the fence row


def test_a_store_behind_the_books_retries_at_the_fences_generation_plus_one(engine):
    """A restored or rebuilt store's counter is behind: activation is stale with the fence's generation, retires
    nothing, and a retry above it succeeds."""
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 7, "T7")
    live = propose(engine, "c1", "T7")
    stale = approvals.activate(engine, "c1", cid, 3, "T3")
    assert (stale.status, stale.generation) == ("stale", 7) and status(engine, live.approval_id) == "pending"
    retry = approvals.activate(engine, "c1", cid, stale.generation + 1, "T8")
    assert (retry.status, retry.set_aside) == ("active", [live.approval_id])


def test_propose_with_a_token_that_is_not_live_is_refused_before_any_card(engine):
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 1, "T1")
    approvals.activate(engine, "c1", cid, 2, "T2")
    before = snapshot(engine)
    for token in ("T1", "never"):
        with pytest.raises(Refusal) as r:
            propose(engine, "c1", token)
        assert (r.value.code, r.value.message) == ("fenced", "That request has ended; ask again.")
    with pytest.raises(Refusal, match="ended"):  # no fence at all: nothing is live
        propose(engine, "c-unfenced", "T1")
    assert snapshot(engine) == before


def test_approving_a_card_whose_request_ended_supersedes_it_and_commits_nothing(engine):
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 1, "T1")
    c = propose(engine, "c1", "T1")
    assert approvals.end_request(engine, "c1", 1, "T1")
    n = count(engine, t.invoice)
    out = approvals.approve(engine, c.approval_id, c.digest)
    assert (out.status, out.text) == ("superseded", approvals.ENDED)
    assert status(engine, c.approval_id) == "superseded" and count(engine, t.invoice) == n
    assert count(engine, m.operation_outcome) == 0
    again = approvals.approve(engine, c.approval_id, c.digest)  # the stored decision
    assert (again.status, again.text) == ("superseded", approvals.ENDED)


def test_end_request_needs_the_fences_own_generation_and_token(engine):
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 3, "T3")
    before = snapshot(engine)
    assert not approvals.end_request(engine, "c1", 2, "T3")
    assert not approvals.end_request(engine, "c1", 3, "T2")
    assert not approvals.end_request(engine, "c-none", 3, "T3")
    assert snapshot(engine) == before
    assert approvals.end_request(engine, "c1", 3, "T3")
    assert fence(engine, "c1")["live_token"] is None and fence(engine, "c1")["generation"] == 3


def test_a_fence_belongs_to_one_company(engine):
    from erpclaw_core.setup import create_company

    other = create_company(engine, "Other Co")
    approvals.activate(engine, "c1", _ours(engine), 1, "T1")
    with pytest.raises(Refusal) as r:
        approvals.activate(engine, "c1", other, 2, "T2")
    assert r.value.code == "fence_company" and fence(engine, "c1")["live_token"] == "T1"


def _ours(engine) -> str:
    with engine.connect() as conn:
        return company_ctx(conn, company_name="Stark Manufacturing").company_id


def test_every_route_that_returns_a_card_returns_only_a_live_or_tokenless_one(engine):
    """The turn's own card, reconciliation's waiting card, the stale path's fresh card and a repeat's stored decision.
    A card stranded on a token that is no longer live (the fence moved by a direct write here) is shown by
    none of them."""
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 1, "T1")
    c = propose(engine, "c1", "T1")
    assert approvals.pending_card(engine, "c1")["id"] == c.approval_id
    with engine.begin() as conn:
        conn.execute(update(m.conversation_fence).where(m.conversation_fence.c.conversation_id == "c1")
                     .values(live_token="T-other"))
    assert approvals.pending_card(engine, "c1") is None
    assert approvals.approve(engine, c.approval_id, c.digest).card is None
    plain = propose(engine, "c2")  # a token-less card in a conversation is not fenced
    assert approvals.pending_card(engine, "c2")["id"] == plain.approval_id


# --- the two-worker interleaving --------------------------------------------------------------------------------

STEPS = ("activate", "propose", "decide", "end")


class Model:
    """The fence's rules, independent of the engine: what each step must do."""

    def __init__(self):
        self.gen, self.live, self.cards = 0, None, {}  # cards: worker -> (token, status)

    def step(self, w, gen, token, what, decide):
        if what == "activate":
            if gen <= self.gen:
                return "stale"
            self.gen, self.live = gen, token
            for k, (tok, st) in self.cards.items():
                if st == "pending" and tok != token:
                    self.cards[k] = (tok, "superseded")
            return "active"
        if what == "propose":
            if self.live != token:
                return "fenced"
            for k, (tok, st) in self.cards.items():
                if st == "pending":
                    self.cards[k] = (tok, "superseded")
            self.cards[w] = (token, "pending")
            return "card"
        if what == "decide":
            if w not in self.cards or self.cards[w][1] != "pending":
                return "none"
            tok = self.cards[w][0]
            if decide == "decline":
                self.cards[w] = (tok, "declined")
            else:
                self.cards[w] = (tok, "consumed" if self.live == tok else "superseded")
            return self.cards[w][1]
        if self.gen == gen and self.live == token:  # end
            self.live = None
        return "ended"


def _interleavings(a: int, b: int):
    for picks in itertools.combinations(range(a + b), a):
        seq, i, j = [], 0, 0
        for k in range(a + b):
            if k in picks:
                seq.append(("w1", STEPS[i])); i += 1
            else:
                seq.append(("w2", STEPS[j])); j += 1
        yield seq


def test_two_workers_in_every_ordering_with_a_crash_at_every_step(engine):
    """Worker 1 (generation 1) and worker 2 (a takeover, generation 2) each activate, propose, decide their card
    (approve or decline) and end; each may crash after any step (its remaining steps never run). Over every
    interleaving the books do exactly what the fence's rules say: at most one live token, no valid pending card
    set aside by a lower generation, no card made or approved under a stale generation."""
    cid = company(engine)
    workers = {"w1": (1, "T1"), "w2": (2, "T2")}
    n = 0
    for decides in (("approve", "decline"), ("decline", "approve")):
        for a in range(len(STEPS) + 1):
            for b in range(len(STEPS) + 1):
                for seq in _interleavings(a, b):
                    n += 1
                    conv, model, made = f"iv{n}", Model(), {}
                    for w, what in seq:
                        gen, token = workers[w]
                        how = decides[0] if w == "w1" else decides[1]
                        want = model.step(w, gen, token, what, how)
                        if what == "activate":
                            got = approvals.activate(engine, conv, cid, gen, token).status
                        elif what == "propose":
                            try:
                                made[w] = propose(engine, conv, token, args={**INVOICE, "submit": False})
                                got = "card"
                            except Refusal as r:
                                got = r.code
                        elif what == "decide":
                            if w not in made or status(engine, made[w].approval_id) != "pending":
                                got = "none"
                            elif how == "decline":
                                got = approvals.decline(engine, made[w].approval_id).status
                            else:
                                o = approvals.approve(engine, made[w].approval_id, made[w].digest)
                                got = {"committed": "consumed"}.get(o.status, o.status)
                        else:
                            approvals.end_request(engine, conv, gen, token)
                            got = "ended"
                        assert got == want, (seq, w, what, got, want)
                    for w, c in made.items():  # the books match the model card by card
                        assert status(engine, c.approval_id) == model.cards[w][1], (seq, w)
                    f = fence(engine, conv)
                    assert (f["generation"], f["live_token"]) == (model.gen, model.live) if f else model.gen == 0
                    if "w1" in made and model.cards["w1"][1] == "consumed":  # worker 1 committed only while live
                        assert not any(x == ("w2", "activate") for x in seq[:seq.index(("w1", "decide"))])
    assert n == 2 * sum(1 for a in range(5) for b in range(5) for _ in _interleavings(a, b))


# --- the lock order (two connections) ---------------------------------------------------------------------------

class HoldFirstLock:
    """Hold `activate` right after it locks the fence row FOR UPDATE."""

    def __init__(self, monkeypatch):
        self.locked, self.go = threading.Event(), threading.Event()
        real = approvals._for_update

        def held(conn, query):
            q = real(conn, query)
            if "conversation_fence" in str(query) and not self.locked.is_set():
                conn.execute(q).all()
                self.locked.set()
                assert self.go.wait(30)
            return q

        monkeypatch.setattr(approvals, "_for_update", held)


def test_activate_then_approve_serialize_on_the_fence_and_never_deadlock(engine, monkeypatch):
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 1, "T1")
    c = propose(engine, "c1", "T1")
    held, box = HoldFirstLock(monkeypatch), {}
    try:
        t1 = run(lambda: approvals.activate(engine, "c1", cid, 2, "T2"), box, "activate")
        assert held.locked.wait(10)
        t2 = run(lambda: approvals.approve(engine, c.approval_id, c.digest), box, "approve")
        assert a_session_waits(engine), "approve did not wait for the fence"
    finally:
        held.go.set()
    t1.join(30); t2.join(30)
    no_deadlock(*box.values())
    assert box["activate"].set_aside == [c.approval_id]
    assert (box["approve"].status, box["approve"].text) == ("superseded", approvals.ENDED)
    assert count(engine, m.operation_outcome) == 0


def test_approve_then_activate_serialize_on_the_fence_and_never_deadlock(engine, monkeypatch):
    from test_approvals_concurrency import Held

    cid = company(engine)
    approvals.activate(engine, "c1", cid, 1, "T1")
    c = propose(engine, "c1", "T1")
    held, box = Held(monkeypatch), {}
    try:
        t1 = run(lambda: approvals.approve(engine, c.approval_id, c.digest), box, "approve")
        assert held.locked.wait(10)
        t2 = run(lambda: approvals.activate(engine, "c1", cid, 2, "T2"), box, "activate")
        assert a_session_waits(engine), "activate did not wait for the fence"
    finally:
        held.go.set()
    t1.join(30); t2.join(30)
    no_deadlock(*box.values())
    assert box["approve"].status == "committed" and box["activate"].set_aside == []


# --- one pending card per conversation --------------------------------------------------------------------------

def test_a_second_request_sets_aside_the_first_card_whatever_its_operation(engine):
    pay = propose(engine, "c1", args=PAYMENT, contract="record_payment")
    inv = propose(engine, "c1")
    assert status(engine, pay.approval_id) == "superseded" and status(engine, inv.approval_id) == "pending"
    assert inv.data["set_aside"] == {"approval_id": pay.approval_id, "title": pay.card["title"]}
    events = approvals.card_events(inv)
    assert [e["type"] for e in events] == ["notice", "card"]
    assert events[0]["code"] == "card_set_aside" and pay.card["title"] in events[0]["text"]
    assert events[1]["replaces"] == pay.approval_id
    before = snapshot(engine)
    out = approvals.approve(engine, pay.approval_id, pay.digest)
    assert (out.status, out.text) == ("superseded", approvals.GONE["superseded"]) and out.card is None
    assert snapshot(engine) == before  # nothing committed, nothing written


def test_the_index_allows_one_pending_card_per_conversation(engine):
    from sqlalchemy.exc import IntegrityError

    a = propose(engine, "c1")
    row = card(engine, a.approval_id)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(m.approval.insert().values({**row, "id": new_id(), "operation_key": new_id(),
                                                     "contract": "record_payment"}))
    with engine.begin() as conn:  # cards outside any conversation are not limited
        for _ in range(2):
            conn.execute(m.approval.insert().values({**row, "id": new_id(), "operation_key": new_id(),
                                                     "conversation_id": None}))


def test_a_turn_shows_the_set_aside_notice_before_its_new_card(engine):
    model = ScriptedModel([Reply("", [call("record_payment", **PAYMENT)]),
                           Reply("", [call("create_invoice", **INVOICE)])])
    c = TestClient(create_app(engine, model, embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    first = c.post(f"/v1/conversations/{cid}/messages", json={"text": "record 100 from Pied Piper by check, oldest first"}).json()
    pay = next(e for e in first["events"] if e["type"] == "card")
    second = c.post(f"/v1/conversations/{cid}/messages",
                    json={"text": "invoice Acme Corporation for 5 copper wire"}).json()["events"]
    kinds = [e["type"] for e in second]
    assert kinds.index("notice") == kinds.index("card") - 1
    notice, new = second[kinds.index("notice")], second[kinds.index("card")]
    assert notice["code"] == "card_set_aside" and new["replaces"] == pay["approval_id"]


# --- the stored decision ----------------------------------------------------------------------------------------

def _stale(engine, c):
    with engine.begin() as conn:
        conn.execute(update(m.item).where(m.item.c.name == "Copper Wire").values(standard_rate="29.00"))
    return approvals.approve(engine, c.approval_id, c.digest)


def test_a_repeat_after_the_stale_path_returns_the_fresh_card_while_it_is_pending(engine):
    c = propose(engine, "c1")
    fresh = _stale(engine, c)
    assert fresh.status == "superseded" and fresh.card and fresh.data["replaces"] == c.approval_id
    before = snapshot(engine)
    again = approvals.approve(engine, c.approval_id, c.digest)
    assert (again.status, again.approval_id, again.digest) == ("superseded", fresh.approval_id, fresh.digest)
    assert snapshot(engine) == before


def test_a_repeat_after_the_fresh_card_committed_returns_its_outcome(engine):
    c = propose(engine, "c1")
    fresh = _stale(engine, c)
    done = approvals.approve(engine, fresh.approval_id, fresh.digest)
    assert done.status == "committed"
    before = snapshot(engine)
    again = approvals.approve(engine, c.approval_id, c.digest)
    assert (again.status, again.operation_key, again.text) == ("committed", done.operation_key, done.text)
    assert snapshot(engine) == before


def test_a_repeat_after_a_question_returns_the_same_question_and_writes_nothing(engine, monkeypatch):
    """The decision keeps the refusal's complete data, choice references included, and the API shows the choices
    by code and label only."""
    c = propose(engine, "c1")
    contract = REGISTRY.contracts["create_invoice"]
    choices = [{"code": "a", "label": "Acme Corporation", "ref": "party:1"},
               {"code": "b", "label": "Acme Industries", "ref": "party:2"}]

    def asks(ctx, inp):
        raise Refusal("Which Acme do you mean?", "customer_ambiguous", ["Acme Corporation", "Acme Industries"],
                      field="customer", question_id="q-1", kind="choose", choices=choices)

    monkeypatch.setattr(contract, "preview", asks)
    app = TestClient(create_app(engine, ScriptedModel([]), embedded_owner=True))
    first = app.post(f"/v1/cards/{c.approval_id}/approve", json={"digest": c.digest}).json()["events"]
    stored = decision(engine, c.approval_id)
    assert stored["data"]["choices"] == choices and stored["data"]["question_id"] == "q-1"  # references kept
    before = snapshot(engine)
    again = app.post(f"/v1/cards/{c.approval_id}/approve", json={"digest": c.digest}).json()["events"]
    assert again == first and snapshot(engine) == before
    [ask] = again
    assert (ask["type"], ask["code"], ask["question_id"], ask["kind"]) == ("ask", "customer_ambiguous", "q-1", "choose")
    assert ask["choices"] == [{"code": "a", "label": "Acme Corporation"}, {"code": "b", "label": "Acme Industries"}]
    assert "party:" not in json.dumps(again)


def test_a_repeat_of_a_declined_or_expired_card_returns_its_message(engine, monkeypatch):
    from datetime import timedelta

    from erpclaw_core.common import now

    d = propose(engine, "c1")
    approvals.decline(engine, d.approval_id)
    assert approvals.approve(engine, d.approval_id, d.digest).text == approvals.GONE["declined"]
    e = propose(engine, "c2")
    monkeypatch.setattr(approvals, "now", lambda: now() + timedelta(minutes=11))
    assert approvals.approve(engine, e.approval_id, e.digest).status == "expired"
    before = snapshot(engine)
    again = approvals.approve(engine, e.approval_id, e.digest)
    assert (again.status, again.text) == ("expired", approvals.GONE["expired"]) and snapshot(engine) == before


# --- the replacement and the fence ------------------------------------------------------------------------------

def test_the_stale_paths_fresh_card_inherits_its_predecessors_token(engine):
    approvals.activate(engine, "c1", company(engine), 1, "T1")
    c = propose(engine, "c1", "T1")
    fresh = _stale(engine, c)
    assert card(engine, fresh.approval_id)["request_card_token"] == "T1"
    assert approvals.pending_card(engine, "c1")["id"] == fresh.approval_id


def test_a_rotation_before_the_fresh_card_makes_no_card_and_ends_the_predecessor(engine, monkeypatch):
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 1, "T1")
    c = propose(engine, "c1", "T1")
    real = approvals._propose

    def rotated(*a, **k):  # a newer worker takes the conversation between the claim and the fresh proposal
        approvals.activate(engine, "c1", cid, 2, "T2")
        return real(*a, **k)

    monkeypatch.setattr(approvals, "_propose", rotated)
    n = count(engine, m.approval)
    out = _stale(engine, c)
    assert (out.status, out.text, out.card) == ("superseded", approvals.ENDED, None)
    assert count(engine, m.approval) == n and status(engine, c.approval_id) == "superseded"
    assert count(engine, m.operation_outcome) == 0


def test_propose_replacing_a_card_after_a_rotation_makes_no_card(engine):
    cid = company(engine)
    approvals.activate(engine, "c1", cid, 1, "T1")
    a = propose(engine, "c1", "T1")
    approvals.activate(engine, "c1", cid, 2, "T2")
    n = count(engine, m.approval)
    with pytest.raises(Refusal) as r:
        propose(engine, "c1", "T1", replaces=a.approval_id)
    assert r.value.code == "fenced" and count(engine, m.approval) == n


# --- replaces ---------------------------------------------------------------------------------------------------

def _foreign_card(engine, a, **change) -> str:
    """A copy of card `a` elsewhere: another conversation, or another company's books."""
    row = card(engine, a.approval_id)
    aid = new_id()
    with engine.begin() as conn:
        conn.execute(m.approval.insert().values({**row, "id": aid, "operation_key": new_id(), **change}))
    return aid


@pytest.mark.parametrize("where", ["another conversation", "another company", "no such card", "no draft there"])
def test_replaces_outside_this_conversation_changes_nothing_and_discloses_nothing(engine, where):
    from erpclaw_core.setup import create_company

    other = create_company(engine, "Other Co")
    ours = _ours(engine)
    seed = propose(engine, "c-seed", company_id=ours)
    target = {"another conversation": lambda: _foreign_card(engine, seed, conversation_id="c-other"),
              "another company": lambda: _foreign_card(engine, seed, conversation_id="c1", company_id=other),
              "no such card": lambda: new_id(),
              "no draft there": lambda: _foreign_card(engine, seed, conversation_id="c1", company_id=other)}[where]()
    who = None
    if where == "no draft there":
        with engine.begin() as conn:
            who = pr.authenticate(conn, admin.add_principal(conn, "dee", ["view", "draft"], [ours]))
    before = snapshot(engine)
    with pytest.raises(Refusal) as r:
        propose(engine, "c1", replaces=target, company_id=ours, principal=who)
    assert (r.value.code, r.value.message) == ("replaces_mismatch",
                                               "That card isn't part of this conversation; nothing was changed.")
    assert r.value.options == [] and r.value.data == {}  # nothing of the other card
    assert snapshot(engine) == before


def test_replaces_a_card_still_pending_supersedes_it(engine):
    a = propose(engine, "c1")
    b = propose(engine, "c1", args={**INVOICE, "items": [{"item": "copper wire", "quantity": 6}]},
                replaces=a.approval_id)
    assert status(engine, a.approval_id) == "superseded" and b.data["set_aside"]["approval_id"] == a.approval_id
    assert b.data["replaces"] == a.approval_id


@pytest.mark.parametrize("ended", ["committed", "declined"])
def test_replaces_a_card_that_already_ended_makes_no_card_and_says_what_happened(engine, ended):
    a = propose(engine, "c1", args=PAYMENT, contract="record_payment")
    if ended == "committed":
        assert approvals.approve(engine, a.approval_id, a.digest).status == "committed"
    else:
        approvals.decline(engine, a.approval_id)
    n = count(engine, m.approval)
    out = propose(engine, "c1", args={**PAYMENT, "amount": "120"}, contract="record_payment", replaces=a.approval_id)
    assert out.status == "replaced_card_ended" and out.card is None and count(engine, m.approval) == n
    assert out.text.endswith("Your change wasn't applied.")
    assert ("already approved" in out.text) == (ended == "committed")
    assert out.data["predecessor"]["status"] == ("consumed" if ended == "committed" else "declined")
    assert approvals.card_events(out)[0]["type"] == "refused"


def test_a_correction_inherits_its_predecessors_token_and_is_fenced_by_it(engine):
    """A correction that names no token takes the card's own, so it cannot commit
    once that request has ended."""
    approvals.activate(engine, "c1", company(engine), 1, "T1")
    a = propose(engine, "c1", "T1")
    b = propose(engine, "c1", args={**INVOICE, "items": [{"item": "copper wire", "quantity": 6}]},
                replaces=a.approval_id)
    assert card(engine, b.approval_id)["request_card_token"] == "T1"
    assert approvals.end_request(engine, "c1", 1, "T1")
    n = count(engine, t.invoice)
    out = approvals.approve(engine, b.approval_id, b.digest)
    assert (out.status, out.text) == ("superseded", approvals.ENDED) and count(engine, t.invoice) == n


def test_a_correction_with_another_requests_token_changes_nothing(engine):
    approvals.activate(engine, "c1", company(engine), 1, "T1")
    a = propose(engine, "c1", "T1")
    before = snapshot(engine)
    with pytest.raises(Refusal) as r:
        propose(engine, "c1", "T9", replaces=a.approval_id)
    assert r.value.code == "replaces_mismatch" and snapshot(engine) == before


def test_a_correction_of_a_tokenless_card_takes_the_callers_token(engine):
    a = propose(engine, "c1")
    approvals.activate(engine, "c1", company(engine), 1, "T1")  # sets the token-less card aside
    approvals.activate(engine, "c2", company(engine), 1, "T2")
    b = propose(engine, "c2")
    c = propose(engine, "c2", "T2", replaces=b.approval_id)
    assert card(engine, c.approval_id)["request_card_token"] == "T2" and status(engine, a.approval_id) == "superseded"


def test_correcting_a_payment_that_settled_its_invoice_reports_the_payment(engine):
    """The predecessor's outcome is checked before the correction is previewed, so
    a payment that already settled its invoice is reported as recorded, never as a refusal of the correction."""
    settle = {"customer": "Pied Piper", "amount": "1120", "invoice": "INV-2026-00006", "method": "check"}
    a = propose(engine, "c1", args=settle, contract="record_payment")
    assert approvals.approve(engine, a.approval_id, a.digest).status == "committed"
    out = propose(engine, "c1", args={**settle, "method": "wire"}, contract="record_payment", replaces=a.approval_id)
    assert out.status == "replaced_card_ended" and "already approved" in out.text and out.card is None


def test_correcting_a_card_that_expired_untouched_reports_the_expiry(engine, monkeypatch):
    """A pending card past its ten minutes is expired whether or not anything touched it: a correction naming it
    makes no card and says it expired, and the card is recorded expired with its decision."""
    from datetime import timedelta

    from erpclaw_core.common import now

    a = propose(engine, "c1")
    monkeypatch.setattr(approvals, "now", lambda: now() + timedelta(minutes=11))
    n = count(engine, m.approval)
    out = propose(engine, "c1", args={**INVOICE, "items": [{"item": "copper wire", "quantity": 6}]},
                  replaces=a.approval_id)
    assert (out.status, out.card) == ("replaced_card_ended", None) and count(engine, m.approval) == n
    assert out.text == approvals.GONE["expired"] + " Your change wasn't applied."
    assert status(engine, a.approval_id) == "expired" and decision(engine, a.approval_id)["status"] == "expired"


class HoldCardLock:
    """Hold `propose(replaces=A)` right after it locks A's row."""

    def __init__(self, monkeypatch):
        self.locked, self.go = threading.Event(), threading.Event()
        real = approvals._for_update

        def held(conn, query):
            q = real(conn, query)
            if "approval.id = " in str(query).replace("\n", " ") and not self.locked.is_set():
                conn.execute(q).all()
                self.locked.set()
                assert self.go.wait(30)
            return q

        monkeypatch.setattr(approvals, "_for_update", held)


def _payments(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM sales_payment")).scalar_one()


def test_an_approval_that_wins_the_race_with_its_correction_commits_once(engine, monkeypatch):
    from test_approvals_concurrency import Held

    a = propose(engine, "c1", args=PAYMENT, contract="record_payment")
    n = _payments(engine)
    held, box = Held(monkeypatch), {}
    try:
        t1 = run(lambda: approvals.approve(engine, a.approval_id, a.digest), box, "approve")
        assert held.locked.wait(10)
        t2 = run(lambda: propose(engine, "c1", args={**PAYMENT, "amount": "120"}, contract="record_payment",
                                 replaces=a.approval_id), box, "propose")
        assert a_session_waits(engine), "the correction did not wait for the card's row"
    finally:
        held.go.set()
    t1.join(30); t2.join(30)
    no_deadlock(*box.values())
    assert box["approve"].status == "committed"
    assert box["propose"].status == "replaced_card_ended" and "already approved" in box["propose"].text
    assert _payments(engine) == n + 1


def test_a_correction_that_wins_the_race_supersedes_the_card_it_replaces(engine, monkeypatch):
    a = propose(engine, "c1", args=PAYMENT, contract="record_payment")
    n = _payments(engine)
    held, box = HoldCardLock(monkeypatch), {}
    try:
        t1 = run(lambda: propose(engine, "c1", args={**PAYMENT, "amount": "120"}, contract="record_payment",
                                 replaces=a.approval_id), box, "propose")
        assert held.locked.wait(10)
        t2 = run(lambda: approvals.approve(engine, a.approval_id, a.digest), box, "approve")
        assert a_session_waits(engine), "the approval did not wait for the card's row"
    finally:
        held.go.set()
    t1.join(30); t2.join(30)
    no_deadlock(*box.values())
    assert box["propose"].status == "card" and box["propose"].data["replaces"] == a.approval_id
    assert box["approve"].status == "superseded" and box["approve"].card is None
    assert _payments(engine) == n


# --- the API ----------------------------------------------------------------------------------------------------

def test_a_busy_conversation_is_refused_with_its_code_and_changes_nothing(engine, monkeypatch):
    app = create_app(engine, ScriptedModel([]), embedded_owner=True)

    def busy(*a, **k):
        raise Refusal("I'm still working on your last message.", "busy")

    monkeypatch.setattr(app.state.assistant, "turn", busy)
    c = TestClient(app)
    before = snapshot(engine)
    body = c.post("/v1/conversations/c1/messages", json={"text": "invoice Acme Corporation"}).json()
    assert body == {"events": [{"type": "refused", "code": "busy", "text": "I'm still working on your last message.",
                                "currency": "USD"}]}
    assert snapshot(engine) == before


def test_the_stale_paths_events_carry_the_code_and_the_replaced_card(engine):
    c = propose(engine, "c1")
    with engine.begin() as conn:
        conn.execute(update(m.item).where(m.item.c.name == "Copper Wire").values(standard_rate="29.00"))
    app = TestClient(create_app(engine, ScriptedModel([]), embedded_owner=True))
    events = app.post(f"/v1/cards/{c.approval_id}/approve", json={"digest": c.digest}).json()["events"]
    assert [(e["type"], e.get("code")) for e in events] == [("refused", "card_changed"), ("card", None)]
    assert events[1]["replaces"] == c.approval_id


# --- the migration ----------------------------------------------------------------------------------------------

def test_older_books_keep_each_conversations_newest_pending_card(engine):
    """Books at 0005 (one pending card per conversation and operation) with two pending cards in one conversation:
    the revision supersedes the older, audits it, and makes the new index; older cards, with no token and no
    decision, decline and answer a repeat as before."""
    from conftest import drop_everything
    from erpclaw_core.migrate import upgrade

    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0005")
        conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                          "VALUES ('c-old', 'Old Co', 'USD', 1, now())"))
        conn.execute(text("INSERT INTO principal (id, name, kind, disabled, grants_version, created_at) "
                          "VALUES ('p-old', 'owner', 'person', false, 1, now())"))
        for perm in m.PERMISSIONS:
            conn.execute(text("INSERT INTO principal_grant VALUES ('p-old', 'c-old', :p)"), {"p": perm})
        for aid, conv, contract, made, st in (("a-old", "conv-1", "record_payment", "10:00", "pending"),
                                              ("a-new", "conv-1", "create_invoice", "11:00", "pending"),
                                              ("a-solo", "conv-2", "create_invoice", "09:00", "pending"),
                                              ("a-done", "conv-3", "create_invoice", "08:00", "superseded")):
            conn.execute(text(
                "INSERT INTO approval (id, principal_id, company_id, conversation_id, contract, contract_version, "
                "policy_version, impl_version, args_json, read_set_json, card_json, nonce, digest, status, "
                "operation_key, created_at, expires_at) VALUES (:id, 'p-old', 'c-old', :conv, :contract, '1', 1, "
                "'x', '{}', '[]', '{}', '', :d, :st, :k, :made, now() + interval '1 hour')"),
                {"id": aid, "conv": conv, "contract": contract, "d": aid.ljust(64, "0"), "k": "k-" + aid,
                 "made": f"2026-10-28 {made}+00", "st": st})
    with engine.begin() as conn:
        upgrade(conn, "0007")
        rows = {r.id: r for r in conn.execute(text("SELECT id, status, decision_json FROM approval"))}
        audits = conn.execute(text("SELECT approval_id, actor, action FROM audit_log")).all()
        index = conn.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_approval_one_pending'")
                             ).scalar_one()
    assert {k: v.status for k, v in rows.items()} == {"a-old": "superseded", "a-new": "pending", "a-solo": "pending",
                                                      "a-done": "superseded"}
    assert json.loads(rows["a-old"].decision_json)["status"] == "superseded"
    assert rows["a-new"].decision_json is None and rows["a-done"].decision_json is None
    assert [(a.approval_id, a.actor, a.action) for a in audits] == [("a-old", "revision 0007", "card_set_aside")]
    assert "(conversation_id)" in index and "contract" not in index and "conversation_id IS NOT NULL" in index
    # an older card ended before decisions were stored answers a repeat with its status's message, as before
    assert approvals.approve(engine, "a-done", "a-done".ljust(64, "0")).text == approvals.GONE["superseded"]
    assert approvals.decline(engine, "a-solo").status == "declined"


# --- what a plugin's write may rely on during a commit -------------------------------------------------------------

@pytest.mark.parametrize("ends", ["committed", "refused"])
def test_a_consumed_card_without_an_outcome_exists_only_inside_its_own_commit__stock_authorize_relies_on_it(
        engine, monkeypatch, ends):
    """The stock plugin's authorize check (erpclaw_stock/service.py) takes the card being committed now to be the one
    the commit flag names that is consumed with no outcome row yet. That holds only because approve claims the card
    and writes its outcome in one transaction, the outcome after the commit: inside the commit the card reads
    consumed with no outcome; another connection does not see it consumed meanwhile; once approve returns, committed
    or refused, it has its outcome. A change to that ordering fails here first and must move stock's check with it
    (a core call is to replace that check)."""
    contract = REGISTRY.contracts["create_invoice"]
    original = contract.commit
    seen: dict = {}

    def observed(ctx, inp, key):
        aid = ctx.extra["commit"]
        seen["inside"] = (ctx.conn.execute(select(m.approval.c.status).where(m.approval.c.id == aid)).scalar_one(),
                          ctx.conn.execute(select(func.count()).select_from(m.operation_outcome).where(
                              m.operation_outcome.c.approval_id == aid)).scalar_one())
        with engine.connect() as other:
            seen["other"] = other.execute(select(m.approval.c.status).where(m.approval.c.id == aid)).scalar_one()
        result = original(ctx, inp, key)
        if ends == "refused":
            raise Refusal("Refused after the commit ran, for the test.", "test_refused")
        return result

    monkeypatch.setattr(contract, "commit", observed)
    c = propose(engine)
    out = approvals.approve(engine, c.approval_id, c.digest)
    assert out.status == ends, out.text
    assert seen["inside"] == ("consumed", 0)
    assert seen["other"] == "pending"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.operation_outcome).where(
            m.operation_outcome.c.approval_id == c.approval_id)).scalar_one() == 1
    assert status(engine, c.approval_id) == "consumed"
