"""Whole-turn deadlines use the store clock, across leases and model retries."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import httpx
import pytest
from sqlalchemy import select, update

from erpclaw_assistant import graph, store
from erpclaw_assistant.model import OpenAICompatModel, Reply, ScriptedModel, call, turn_budget
from erpclaw_assistant.muse import LocalModel, MuseModel
from erpclaw_core import approvals, models as m, principals as pr
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.masters import create_party
from erpclaw_core.plugins import REGISTRY
from test_model_profiles import OPENROUTER, completion, model
from test_muse import fcall, resp, text
from test_store_seq import finish, start
from test_turn_failure_codes import client, durable_refusal, plain_sentence, post


TIMEOUT = "This turn took too long. Please try again or ask for something smaller."
INVOICE = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}])
SAID = "invoice Acme Corporation for 2 copper wire"


@pytest.fixture
def clock(monkeypatch):
    current = [datetime(2026, 11, 8, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "_now", lambda: current[0])
    return current


def row(cid="c"):
    with sqlite3.connect(store.path()) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone())


def test_legacy_deadline_is_null_until_a_fresh_claim(engine, clock):
    """Mutation: omit the add-if-missing column or backfill a deadline."""
    c, _ = start(engine)
    store.release(c)
    with sqlite3.connect(store.path()) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(conversation)")}
        if "turn_deadline_at" in columns:
            conn.execute("ALTER TABLE conversation DROP COLUMN turn_deadline_at")
        conn.execute("UPDATE store_version SET version = 5")
    store._ready.discard(str(store.path()))
    with store._open() as conn:
        assert "turn_deadline_at" in {r["name"] for r in conn.execute("PRAGMA table_info(conversation)")}
    assert row()["turn_deadline_at"] is None
    c, _ = start(engine)
    assert row()["turn_deadline_at"] == store._stamp(clock[0] + store.TURN_TIMEOUT)
    store.release(c)


def test_deadline_survives_renewals_and_same_holder_generation_changes(engine, clock):
    """Mutation: move the deadline in renew, catch_up, next_generation, or a same-holder claim."""
    c, _ = start(engine)
    deadline = row()["turn_deadline_at"]
    assert deadline == store._stamp(clock[0] + store.TURN_TIMEOUT)
    for action in (store.renew, lambda c: store.catch_up(c, 20), store.next_generation):
        clock[0] += timedelta(seconds=100)
        result = action(c)
        assert result
        if isinstance(result, store.Claim):
            c = result
        assert row()["turn_deadline_at"] == deadline
    saved = row()
    c = store.claim("c", "same holder", turn_id=c.turn_id,
                    principal_id=saved["owner_principal_id"], principal_name="owner")
    assert c is not None and row()["turn_deadline_at"] == deadline
    clock[0] += timedelta(seconds=100)
    assert store.renew(c)
    assert row()["renewed_at"] == store._stamp(clock[0])
    assert row()["turn_deadline_at"] == deadline


@pytest.mark.parametrize("action", ["renew", "catch_up", "next_generation", "release", "finalise"])
def test_failed_ownership_checks_leave_the_new_holders_deadline(engine, clock, action):
    """Mutation: a failed ownership check clears the current holder's deadline."""
    old, rid = start(engine)
    clock[0] += store.LEASE + timedelta(seconds=1)
    current, _ = start(engine)
    before = row()
    assert before["turn_deadline_at"] == store._stamp(clock[0] + store.TURN_TIMEOUT)
    if action == "finalise":
        assert not finish(old, rid, [{"type": "answer", "text": "stale"}])
    elif action == "catch_up":
        assert store.catch_up(old, current.generation) is None
    else:
        assert not getattr(store, action)(old)
    assert row() == before
    assert store.events(rid) == []


@pytest.mark.parametrize("action", ["release", "finalise"])
def test_ending_the_claim_clears_the_deadline_and_a_new_claim_resets_it(engine, clock, action):
    """Mutation: retain a released deadline, or reuse it for a new holder."""
    c, rid = start(engine)
    original = row()["turn_deadline_at"]
    if action == "release":
        store.release(c)
    else:
        assert finish(c, rid, [])
    assert row()["turn_deadline_at"] is None
    clock[0] += timedelta(seconds=20)
    start(engine)
    assert row()["turn_deadline_at"] != original
    assert row()["turn_deadline_at"] == store._stamp(clock[0] + store.TURN_TIMEOUT)


class TimedServer:
    def __init__(self, clock, route, duration, *, statuses=()):
        self.clock, self.route, self.duration = clock, route, duration
        self.statuses = list(statuses)
        self.timeouts, self.started = [], []

    def handler(self, request):
        self.started.append(self.clock[0])
        self.timeouts.append(request.extensions["timeout"])
        self.clock[0] += timedelta(seconds=self.duration)
        if self.statuses:
            status = self.statuses.pop(0)
            if status == "timeout":
                raise httpx.ReadTimeout("scripted timeout", request=request)
            return httpx.Response(status, headers={"retry-after": "1"}, json={"error": {"message": "busy"}})
        if len(self.started) < graph.MAX_MODEL_REQUESTS:
            if self.route == "muse":
                reply = resp([fcall("bad", "missing_tool", "{}")])
            else:
                reply = completion("", calls=[{"id": "bad", "type": "function", "function": {
                    "name": "missing_tool", "arguments": "{}"}}])
        else:
            reply = resp([text("Hello.")]) if self.route == "muse" else completion("Hello.")
        return httpx.Response(200, json=reply)


def timed_model(fake, timeout=180):
    def sleep(seconds):
        fake.clock[0] += timedelta(seconds=seconds)

    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    if fake.route == "muse":
        mdl = MuseModel(api_key="synthetic", base_url="https://muse.test/v1", http_client=http,
                        timeout_s=timeout, sleep=sleep)
    elif fake.route == "local":
        mdl = LocalModel(OpenAICompatModel(base_url="https://local.test/v1", http_client=http, timeout_s=timeout))
    else:
        mdl = model(OPENROUTER, fake, timeout_s=timeout)
        mdl._sleep = sleep
    return mdl


def test_claim_lost_mid_call_cannot_finalise_timeout(engine, clock):
    """Mutation: finalise skips the ownership fence and writes a lost holder's timeout."""
    old, rid = start(engine)
    original = row()
    takeover = {}
    fake = TimedServer(clock, "compatible", 0)

    def lose_claim(request):
        clock[0] += store.LEASE + timedelta(seconds=1)
        current = store.claim("c", "take over", principal_id=original["owner_principal_id"],
                              principal_name="owner")
        assert current is not None and current.took_over
        assert current.turn_id != old.turn_id and current.generation > old.generation
        assert clock[0] < datetime.fromisoformat(original["turn_deadline_at"])
        assert store.turn_remaining(old) == 0
        assert store.turn_remaining(current) == store.TURN_TIMEOUT.total_seconds()
        takeover["conversation"] = row()
        takeover["request"] = store.get_request(rid)
        raise httpx.ReadTimeout("call outlived its claim", request=request)

    fake.handler = lose_claim
    mdl = timed_model(fake)
    with turn_budget(lambda: store.turn_remaining(old)):
        with pytest.raises(Refusal) as raised:
            mdl.complete([{"role": "user", "content": "hello"}], [])
    assert raised.value.code == "turn_timeout"
    assert raised.value.message == TIMEOUT
    assert takeover["request"]["state"] == "open"
    assert not store.finalise(old, rid, [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT}],
                              pending_question=None, pending_card=None, state="ended", why="refused",
                              serial_now=0, bindings_now={}, intent_state="unused")
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute("SELECT count(*) FROM request_event WHERE request_id = ?", (rid,)).fetchone() == (0,)
    assert store.get_request(rid) == takeover["request"]
    assert row() == takeover["conversation"]


@pytest.mark.parametrize("route", ["compatible", "muse", "local"])
def test_short_calls_exhaust_one_deadline_despite_renewals(engine, monkeypatch, clock, route):
    """Mutations: use renewed_at as origin, move the deadline in renew/catch_up, omit checks, or uncap timeouts."""
    initial = clock[0]
    fake = TimedServer(clock, route, 170)
    c, cid = client(engine, timed_model(fake))
    activate = approvals._activate
    activations = []

    def behind(engine, cid, company_id, generation, token):
        activations.append(generation)
        if len(activations) == 1:
            activate(engine, cid, company_id, generation + 10, "earlier fence")
            clock[0] += timedelta(seconds=20)
        return activate(engine, cid, company_id, generation, token)

    monkeypatch.setattr(approvals, "_activate", behind)
    renewals = []
    renew = store.renew

    def record(claim):
        renewals.append(clock[0])
        return renew(claim)

    monkeypatch.setattr(store, "renew", record)
    events = post(c, cid)
    assert events == [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT, "currency": "USD"}]
    assert len(fake.started) == 3 < graph.MAX_MODEL_REQUESTS
    assert len(activations) == 2 and activations[1] > activations[0]
    assert clock[0] - initial > store.TURN_TIMEOUT
    assert max(renewals) > initial + store.TURN_TIMEOUT  # the next pre-call renewal reached the deadline
    for at, limits in zip(fake.started, fake.timeouts):
        expected = min(180, (initial + store.TURN_TIMEOUT - at).total_seconds())
        assert limits and all(value == expected for value in limits.values())
    plain_sentence(events[0]["text"])
    durable_refusal(cid, events, "turn_timeout")
    assert row(cid)["turn_deadline_at"] is None


@pytest.mark.parametrize("route", ["compatible", "muse", "local"])
def test_inside_budget_calls_complete_and_do_not_mutate_the_shared_client(engine, clock, route):
    """Mutation: shorten the fixed budget to one per-call timeout, or change shared client settings."""
    assert store.TURN_TIMEOUT == timedelta(seconds=480)
    fake = TimedServer(clock, route, 110)
    mdl = timed_model(fake)
    before = mdl.client.timeout
    c, cid = client(engine, mdl)
    assert [e["type"] for e in post(c, cid)] == ["answer"]
    assert len(fake.started) == graph.MAX_MODEL_REQUESTS
    assert fake.timeouts[-1]["read"] == 150
    assert mdl.client.timeout == before
    assert row(cid)["turn_deadline_at"] is None


@pytest.mark.parametrize("route", ["compatible", "muse"])
@pytest.mark.parametrize("status", [503, "timeout"])
def test_retry_recomputes_remaining_budget(engine, monkeypatch, clock, route, status):
    """Mutation: reuse the first attempt's timeout after the retry wait."""
    monkeypatch.setattr(store, "TURN_TIMEOUT", timedelta(seconds=5))
    fake = TimedServer(clock, route, 2, statuses=[status])
    mdl = timed_model(fake)
    if route == "muse":
        # Make the no-header retry delay deterministic without sleeping.
        monkeypatch.setattr("erpclaw_assistant.muse._retry_after", lambda *args: 1.0)
    c, cid = client(engine, mdl)
    events = post(c, cid)
    assert [limits["read"] for limits in fake.timeouts] == [5, 2]
    assert events == [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT, "currency": "USD"}]
    durable_refusal(cid, events, "turn_timeout")


@pytest.mark.parametrize("route", ["compatible", "muse", "local"])
def test_expired_in_flight_call_is_timeout_without_a_retry(engine, monkeypatch, clock, route):
    """Mutation: retry an exhausted call, or map deadline expiry to model_unavailable."""
    monkeypatch.setattr(store, "TURN_TIMEOUT", timedelta(seconds=5))
    fake = TimedServer(clock, route, 5, statuses=["timeout"])
    c, cid = client(engine, timed_model(fake))
    events = post(c, cid)
    assert len(fake.started) == 1 and fake.timeouts[0]["read"] == 5
    assert events == [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT, "currency": "USD"}]
    durable_refusal(cid, events, "turn_timeout")


@pytest.mark.parametrize("route", ["compatible", "muse"])
def test_per_call_timeout_inside_budget_keeps_model_unavailable(engine, clock, route):
    """Mutation: map any per-call timeout to turn_timeout, or extend a shorter configured timeout."""
    fake = TimedServer(clock, route, 2, statuses=["timeout", "timeout"])
    c, cid = client(engine, timed_model(fake, timeout=2))
    events = post(c, cid)
    assert [limits["read"] for limits in fake.timeouts] == [2, 2]
    assert events[0]["code"] == "model_unavailable"
    assert clock[0] - fake.started[0] < timedelta(seconds=6)
    durable_refusal(cid, events, "model_unavailable")
    assert "turn_timeout" in graph.MODEL_REFUSALS


def test_expiry_before_propose_makes_no_card(engine, monkeypatch, clock):
    """Mutation: remove the deadline check beside the pre-propose renewal."""
    monkeypatch.setattr(store, "TURN_TIMEOUT", timedelta(seconds=5))
    save = store.save_resolution

    def expire(*args, **kwargs):
        result = save(*args, **kwargs)
        clock[0] += timedelta(seconds=5)
        return result

    monkeypatch.setattr(store, "save_resolution", expire)
    c, cid = client(engine, ScriptedModel([Reply("", [INVOICE])]))
    with engine.connect() as conn:
        before = conn.execute(select(m.approval)).mappings().all()
    events = post(c, cid, SAID)
    assert events == [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT, "currency": "USD"}]
    with engine.connect() as conn:
        assert conn.execute(select(m.approval)).mappings().all() == before
    durable_refusal(cid, events, "turn_timeout")


def test_timeout_keeps_a_previously_proposed_card_pending(engine, clock):
    """Mutation: finalise the timeout by setting aside the existing card or ending its request."""
    c, cid = client(engine, ScriptedModel([Reply("", [INVOICE])]))
    [card] = post(c, cid, SAID)
    pending = store.open_request(cid)
    clock[0] += timedelta(seconds=10)
    fake = TimedServer(clock, "compatible", 170)
    mdl = timed_model(fake)
    # A second app uses the same store and books, with a different scripted model.
    from erpclaw_api.app import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app(engine, mdl, embedded_owner=True))
    events = post(c, cid, "hello")
    assert events == [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT +
                       " Your approval card is still pending.", "currency": "USD"}]
    assert store.open_request(cid)["pending_card"] == card["approval_id"]
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == card["approval_id"])).scalar_one() == "pending"
    assert store.events(pending["id"])[-1] == {k: v for k, v in events[0].items() if k != "currency"}
    assert row(cid)["claimed_by"] is None and row(cid)["turn_deadline_at"] is None
    approved = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]})
    assert approved.status_code == 200
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == card["approval_id"])).scalar_one() == "consumed"


@pytest.mark.parametrize("during_call", ["replacement", "gone", "other_request"])
def test_timeout_reconciles_only_a_live_card_of_its_request(engine, monkeypatch, clock, during_call):
    """Mutations: compare only the stored card id; adopt any live card regardless of its request token."""
    monkeypatch.setattr(store, "TURN_TIMEOUT", timedelta(seconds=5))
    changed = {}
    with engine.connect() as conn:
        owner = pr.owner(conn)["id"]
    ownership = lambda conversation: store.conversation_owner(conversation) == owner

    def fence():
        with engine.connect() as conn:
            return dict(conn.execute(select(m.conversation_fence).where(
                m.conversation_fence.c.conversation_id == cid)).mappings().one())

    class RacingModel(ScriptedModel):
        def complete(self, messages, tools):
            reply = super().complete(messages, tools)
            if len(self.seen) == 2:
                assert store.get_request(req["id"])["pending_card"] == card["approval_id"]
                assert fence()["live_token"] == req["token"]
                if during_call == "replacement":
                    with engine.begin() as conn:
                        conn.execute(update(m.item).where(m.item.c.name == "Copper Wire").values(
                            standard_rate="29.00"))
                    fresh = approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=ownership)
                    assert fresh.status == "superseded" and fresh.card
                    assert fresh.approval_id != card["approval_id"]
                    changed["card"] = {"approval_id": fresh.approval_id, "digest": fresh.digest}
                elif during_call == "gone":
                    assert approvals.decline(engine, card["approval_id"], ownership_check=ownership).status == "declined"
                    assert approvals.pending_card(engine, cid, ownership_check=ownership) is None
                else:
                    live = fence()
                    token = "another request token"
                    assert token != req["token"]
                    assert approvals._activate(engine, cid, live["company_id"], live["generation"] + 1,
                                              token).status == "active"
                    with engine.connect() as conn:
                        original = conn.execute(select(m.approval).where(
                            m.approval.c.id == card["approval_id"])).mappings().one()
                    fresh = approvals.propose(engine, REGISTRY.contracts[original["contract"]],
                                              json.loads(original["args_json"]), cid, request_card_token=token,
                                              ownership_check=ownership)
                    changed["card"] = {"approval_id": fresh.approval_id, "digest": fresh.digest}
                changed["fence"] = fence()
                if during_call != "gone":
                    live = approvals.pending_card(engine, cid, ownership_check=ownership)
                    assert live["id"] == changed["card"]["approval_id"]
                    assert live["request_card_token"] == changed["fence"]["live_token"]
                    assert (live["request_card_token"] == req["token"]) == (during_call == "replacement")
                clock[0] += store.TURN_TIMEOUT
            return reply

    c, cid = client(engine, RacingModel([Reply("", [INVOICE]), Reply("Hello.")]))
    [card] = post(c, cid, SAID)
    req = store.open_request(cid)
    before = store.events(req["id"])
    events = post(c, cid, "please check this")
    kept = during_call == "replacement"
    saved = store.get_request(req["id"])
    assert saved["pending_card"] == (changed["card"]["approval_id"] if kept else None)
    assert saved["state"] == ("open" if kept else "ended")
    assert saved["ended_why"] == (None if kept else "refused")
    assert (store.open_request(cid) is not None) == kept
    notices = [] if kept else [{"type": "notice", "code": "card_set_aside", "text": graph.say("card_unchanged")}]
    assert events == notices + [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT +
                                (" Your approval card is still pending." if kept else ""), "currency": "USD"}]
    assert store.events(req["id"]) == before + [{k: v for k, v in e.items() if k != "currency"} for e in events]
    assert row(cid)["claimed_by"] is None and row(cid)["turn_deadline_at"] is None
    if during_call == "gone":
        assert fence()["live_token"] is None
        assert fence()["generation"] == changed["fence"]["generation"]
    else:
        assert fence() == changed["fence"]
        fresh = changed["card"]
        approved = c.post(f"/v1/cards/{fresh['approval_id']}/approve", json={"digest": fresh["digest"]})
        assert approved.status_code == 200
        assert approved.json()["events"][0]["status"] == "committed"
        with engine.connect() as conn:
            assert conn.execute(select(m.approval.c.status).where(
                m.approval.c.id == fresh["approval_id"])).scalar_one() == "consumed"


@pytest.mark.parametrize("expired", [False, True])
def test_cancel_declines_a_pending_card_even_past_the_deadline(engine, monkeypatch, clock, expired):
    """Mutation: the old order times out before declining the owner's card."""
    from bookdiff import assert_changes, snapshot

    monkeypatch.setattr(store, "TURN_TIMEOUT", timedelta(seconds=5))
    mdl = ScriptedModel([Reply("", [INVOICE])])
    c, cid = client(engine, mdl)
    [card] = post(c, cid, SAID)
    rid = store.open_request(cid)["id"]
    before = snapshot(engine)
    previous_events = store.events(rid)
    match_answer = graph.resolve.match_answer

    def arriving(pending, text, *args, **kwargs):
        matched = match_answer(pending, text, *args, **kwargs)
        assert matched == ("control", "cancel")
        clock[0] += timedelta(seconds=6 if expired else 1)
        assert (clock[0] > datetime.fromisoformat(row(cid)["turn_deadline_at"])) == expired
        return matched

    monkeypatch.setattr(graph.resolve, "match_answer", arriving)
    events = post(c, cid, "cancel")
    assert events == [{"type": "answer", "text": "Cancelled; nothing was saved.",
                       "checked": "engine", "currency": "USD"}]
    assert all("still pending" not in e.get("text", "") for e in events)
    assert len(mdl.seen) == 1
    req = store.get_request(rid)
    assert req["state"] == "ended" and req["ended_why"] == "cancelled"
    assert req["pending_card"] is None and req["pending_question"] is None
    assert store.open_request(cid) is None
    assert store.events(rid) == previous_events + [{k: v for k, v in e.items() if k != "currency"} for e in events]
    assert row(cid)["claimed_by"] is None and row(cid)["turn_deadline_at"] is None
    after = snapshot(engine)
    declined = after["approval"][(card["approval_id"],)]
    assert declined["status"] == "declined" and declined["decided_at"] is not None
    assert json.loads(declined["decision_json"])["status"] == "declined"
    assert_changes(before, after, {"approval": {"changed": [({"id": card["approval_id"]}, {
        key: declined[key] for key in ("status", "decided_at", "decision_json")})]}})


@pytest.mark.parametrize("expiry", ["model", "checkpoint", None])
def test_money_name_correction_timeout_tracks_the_books_card_status(engine, monkeypatch, clock, expiry):
    """Mutations: omit the confirmation deadline check; trust the stored pending_card on timeout."""
    monkeypatch.setattr(store, "TURN_TIMEOUT", timedelta(seconds=5))
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", "Discount Depot", None, None)

    class CorrectionModel(ScriptedModel):
        def complete(self, messages, tools):
            reply = super().complete(messages, tools)
            if len(self.seen) == 2 and expiry == "model":
                clock[0] += store.TURN_TIMEOUT
            return reply

    correction = call("create_invoice", customer="Discount Depot", items=[{"item": "copper wire", "quantity": 2}])
    c, cid = client(engine, CorrectionModel([Reply("", [INVOICE]), Reply("", [correction])]))
    [card] = post(c, cid, SAID)
    rid = store.open_request(cid)["id"]
    before = store.events(rid)
    save = store.save_resolution
    checkpoints = []

    def expire_after_checkpoint(claim, request_id, resolution):
        saved = save(claim, request_id, resolution)
        if saved and (resolution.get("pending") or {}).get("span_id") and not checkpoints:
            checkpoints.append(resolution)
            if expiry == "checkpoint":
                clock[0] += store.TURN_TIMEOUT
        return saved

    monkeypatch.setattr(store, "save_resolution", expire_after_checkpoint)
    events = post(c, cid, "Invoice Discount Depot instead")
    req = store.get_request(rid)
    notice = {"type": "notice", "code": "card_set_aside",
              "text": graph.SET_ASIDE.format(title=card["card"]["title"])}
    if expiry == "model":
        assert events == [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT +
                           " Your approval card is still pending.", "currency": "USD"}]
        assert not checkpoints
        assert req["pending_card"] == card["approval_id"] and req["state"] == "open"
        status = "pending"
    elif expiry == "checkpoint":
        assert events == [notice, {"type": "refused", "code": "turn_timeout", "text": TIMEOUT, "currency": "USD"}]
        assert checkpoints
        assert req["pending_card"] is None and req["state"] == "ended"
        assert store.open_request(cid) is None
        status = "superseded"
    else:
        assert events[0] == notice
        assert [(e["type"], e.get("code")) for e in events] == [
            ("notice", "card_set_aside"), ("ask", "money_name_ambiguous")]
        assert [choice["label"] for choice in events[1]["data"]["choices"]] == [
            "Discount Depot", "no, I mean tax or a discount"]
        assert req["pending_card"] is None and req["state"] == "open"
        assert req["pending_question"]["span_id"] and checkpoints
        status = "superseded"
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == card["approval_id"])).scalar_one() == status
    assert store.events(rid) == before + [{k: v for k, v in e.items() if k != "currency"} for e in events]
    assert row(cid)["claimed_by"] is None and row(cid)["turn_deadline_at"] is None


@pytest.mark.parametrize("unchanged", [False, True])
def test_expiry_during_final_checkpoint_does_not_set_aside_a_pending_card(engine, monkeypatch, clock, unchanged):
    """Mutations: omit the final set-aside deadline check or retain its premature notice on timeout."""
    monkeypatch.setattr(store, "TURN_TIMEOUT", timedelta(seconds=5))
    reply = Reply("Hello.") if unchanged else Reply("", [call("get_metric", metric="receivables")])
    c, cid = client(engine, ScriptedModel([Reply("", [INVOICE]), reply]))
    [card] = post(c, cid, SAID)
    rid = store.open_request(cid)["id"]
    save = store.save_resolution
    checkpoints = []

    def expire(claim, request_id, resolution):
        saved = save(claim, request_id, resolution)
        checkpoints.append(saved)
        if len(checkpoints) == 2:
            clock[0] += store.TURN_TIMEOUT
        return saved

    monkeypatch.setattr(store, "save_resolution", expire)
    timeout = graph.Assistant._timeout
    timeout_events = []

    def record_timeout(self, claim, request, events):
        timeout_events.append(list(events))
        return timeout(self, claim, request, events)

    monkeypatch.setattr(graph.Assistant, "_timeout", record_timeout)
    events = post(c, cid, "please check this" if unchanged else "who owes us money?")
    assert checkpoints == [True, True]
    assert len(timeout_events) == 1
    if unchanged:
        assert timeout_events[0] == [{"type": "notice", "code": "card_set_aside",
                                     "text": graph.say("card_unchanged")}]
    assert events == [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT +
                       " Your approval card is still pending.", "currency": "USD"}]
    req = store.get_request(rid)
    assert req["pending_card"] == card["approval_id"] and req["state"] == "open"
    assert store.events(rid)[-1] == {k: v for k, v in events[0].items() if k != "currency"}
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == card["approval_id"])).scalar_one() == "pending"


@pytest.mark.parametrize("retry", [False, True])
def test_expired_activation_does_not_supersede_a_pending_card(engine, monkeypatch, clock, retry):
    """Mutation: activate or retry activation after the deadline without checking it."""
    monkeypatch.setattr(store, "TURN_TIMEOUT", timedelta(seconds=5))
    c, cid = client(engine, ScriptedModel([Reply("", [INVOICE])]))
    [card] = post(c, cid, SAID)
    rid = store.open_request(cid)["id"]
    activations = []
    activate = approvals._activate

    def behind(engine, conversation_id, company_id, generation, token):
        activations.append(generation)
        if len(activations) == 1:
            activate(engine, conversation_id, company_id, generation + 10, token)
        return activate(engine, conversation_id, company_id, generation, token)

    monkeypatch.setattr(approvals, "_activate", behind)
    if retry:
        catch_up = store.catch_up

        def expire_after_catch_up(*args):
            claim = catch_up(*args)
            clock[0] += store.TURN_TIMEOUT
            return claim

        monkeypatch.setattr(store, "catch_up", expire_after_catch_up)
    else:
        begin = graph.Assistant._activate

        def expire_before_activation(self, *args):
            clock[0] += store.TURN_TIMEOUT
            return begin(self, *args)

        monkeypatch.setattr(graph.Assistant, "_activate", expire_before_activation)
    events = post(c, cid)
    assert len(activations) == int(retry)
    assert events == [{"type": "refused", "code": "turn_timeout", "text": TIMEOUT +
                       " Your approval card is still pending.", "currency": "USD"}]
    req = store.get_request(rid)
    assert req["pending_card"] == card["approval_id"] and req["state"] == "open"
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status).where(
            m.approval.c.id == card["approval_id"])).scalar_one() == "pending"


@pytest.mark.parametrize("code", ["model_rate_limited", "answer_interrupted", "turn_timeout"])
def test_built_failure_codes_have_the_frozen_turn_shape_and_feature(engine, code):
    """Mutation: omit the built feature or a closed-set code, or replace readable event text with a new shape."""
    from erpclaw_core.common import Refusal
    from test_turn_failure_codes import BUSY, INTERRUPTED, FailingModel

    sentence = {"model_rate_limited": BUSY, "answer_interrupted": INTERRUPTED, "turn_timeout": TIMEOUT}[code]
    c, cid = client(engine, FailingModel([Refusal(sentence, code)]))
    assert code in graph.MODEL_REFUSALS
    [event] = post(c, cid)
    assert event == {"type": "refused", "code": code, "text": sentence, "currency": "USD"}
    assert isinstance(event["text"], str) and event["text"].strip()  # an older client can ignore the unknown code
    assert not {"turn_id", "seq", "at", "detail"} & event.keys()
    assert json.loads(json.dumps(event)) == event
    assert c.get("/v1/me").json()["features"].count("turn_failure_codes") == 1
