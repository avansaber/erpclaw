"""Mixed messages answer through every turn path without cards or book changes."""
from __future__ import annotations

import pytest

from bookdiff import assert_changes, snapshot
from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant, ReconciliationIncomplete
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core.contracts import planned_reply
from erpclaw_core.plugins import REGISTRY, disable_plugin
from erpclaw_core import approvals
from erpclaw_core.common import Refusal
from test_catalogue import chat, find_then
from test_mixed_reply import ONE_CHANGE, planned_text
from test_turn_failure_codes import FailingModel, durable_refusal
from test_hosted_guards import _shapes, _stored, openrouter, unmark
from test_model_profiles import FakeServer
from erpclaw_core.db import DEMO_MARK, EVAL_MARK
from test_own_books import own_books, Q_DATES, OwnerWords, setup_words


def invoice(customer="Acme Corporation"):
    return call("create_invoice", customer=customer, items=[{"item": "copper wire", "quantity": 5}])


@pytest.mark.parametrize("supplier_first", [True, False])
@pytest.mark.parametrize("path", ["text", "direct", "two-calls", "op.create_invoice", "planned.purchasing",
                                  "none", "unsure", "op.invented", "model-search-omits-supplier"])
def test_mixed_turn_answers_combined(engine, as_released, supplier_first, path):
    customer = "create a new invoice for Acme Corporation"
    if path == "two-calls":
        customer = "invoice Acme Corporation for 5 copper wire and Hooli for 5 copper wire"
    clauses = ["pay the supplier bill", customer]
    said = " and ".join(clauses if supplier_first else reversed(clauses))
    before = snapshot(engine)
    if path == "text":
        replies = [Reply("Okay.", []), Reply("Okay.", [])]
    elif path == "direct":
        replies = [Reply("", [invoice()])]
    elif path == "two-calls":
        replies = [Reply("", [invoice(), invoice("Hooli")])]
    elif path == "model-search-omits-supplier":
        replies = find_then("op.create_invoice", "invoice Acme")
    else:
        replies = find_then(path, said)
    events, _ = chat(engine, replies, said)
    assert not any(e.get("type") in {"card", "ask"} for e in events)
    assert not any(e.get("code") == "changes_ambiguous" for e in events)
    last = events[-1]
    assert last.get("type") == "answer" and last.get("outcome") == "c"
    assert last.get("capability") == "planned.purchasing"
    assert last.get("text") == planned_text(engine) + " " + ONE_CHANGE
    assert_changes(before, snapshot(engine), {})
    assert any(row[4] == said for row in store.demand())
    # No pending choice: resending the invoice alone proposes its ordinary card.
    if path == "direct":
        single = "invoice Acme Corporation for 5 copper wire"
        events = Assistant(engine, ScriptedModel([Reply("", [invoice()])])).turn("single", single)
        assert [e["type"] for e in events] == ["card"]


def test_pure_supplier_turn_stays_planned_only(engine, as_released):
    said = "pay the supplier bill"
    before = snapshot(engine)
    events, _ = chat(engine, find_then("planned.purchasing", said), said)
    assert not any(e.get("type") == "card" for e in events)
    assert events[-1].get("outcome") == "c"
    assert events[-1].get("capability") == "planned.purchasing"
    assert events[-1].get("text") == planned_text(engine)
    assert_changes(before, snapshot(engine), {})


def early_exit_reply(engine, path):
    if path == "completion":
        return Reply("Done.", [])
    if path == "reports":
        return Reply("", [call("get_metric", metric="receivables")])
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    return Reply("", [invoice()])


@pytest.mark.parametrize("supplier_first", [True, False])
@pytest.mark.parametrize("path", ["completion", "reports", "sales-off"])
def test_mixed_early_exit_answers_combined(engine, as_released, supplier_first, path):
    clauses = ["pay the supplier bill", "create a new invoice for Acme Corporation"]
    said = " and ".join(clauses if supplier_first else reversed(clauses))
    reply = early_exit_reply(engine, path)
    before = snapshot(engine)
    events, _ = chat(engine, [reply], said)
    assert not any(e.get("type") in {"card", "ask"} for e in events)
    last = events[-1]
    assert last.get("type") == "answer" and last.get("outcome") == "c"
    assert last.get("capability") == "planned.purchasing"
    assert last.get("text") == planned_text(engine) + " " + ONE_CHANGE
    assert_changes(before, snapshot(engine), {})
    assert any(row[2:5] == ("c", "planned.purchasing", said) for row in store.demand())


@pytest.mark.parametrize("path", ["completion", "reports", "sales-off"])
def test_non_mixed_early_exit_keeps_its_reply(engine, as_released, path):
    reply = early_exit_reply(engine, path)
    said = {"completion": "pay the supplier bill", "reports": "who owes us money",
            "sales-off": "invoice Acme Corporation for 5 copper wire"}[path]
    before = snapshot(engine)
    events, _ = chat(engine, [reply], said)
    assert len(events) == 1
    last = events[-1]
    if path == "completion":
        assert last == {"type": "answer", "text": "Nothing has been saved yet.", "checked": "replaced",
                        "currency": "USD"}
    elif path == "reports":
        assert last.get("type") == "answer" and last.get("outcome") == "c"
        assert last.get("capability") == "planned.reports"
        assert last.get("text") == planned_reply(REGISTRY.contracts["get_metric"])
    else:
        assert last.get("type") == "refused" and last.get("code") == "plugin_not_enabled"
        assert last.get("capability") == "available.op.create_invoice"
    assert_changes(before, snapshot(engine), {})


MIXED = "pay the supplier bill and invoice the customer"
MODEL_ERRORS = ["answer_interrupted", "model_input_invalid", "model_refused", "turn_timeout",
                "model_unavailable", "model_rate_limited"]


def round3_combined(engine, events, said):
    assert len(events) == 1, events
    assert events[0].get("type") == "answer" and events[0].get("outcome") == "c", events
    assert events[0].get("capability") == "planned.purchasing"
    assert events[0]["text"] == planned_text(engine) + " " + ONE_CHANGE
    assert store.demand()[-1][2:5] == ("c", "planned.purchasing", said)


@pytest.mark.parametrize("supplier_first", [True, False])
@pytest.mark.parametrize("code", MODEL_ERRORS)
def test_round3_mixed_never_requests_a_failing_model(engine, as_released, supplier_first, code):
    clauses = ["pay the supplier bill", "create a new invoice for Acme Corporation"]
    said = " and ".join(clauses if supplier_first else reversed(clauses))
    failure = Reply("Partial answer", [], finish="length") if code == "answer_interrupted" else Refusal(
        "The model could not answer.", code)
    model = FailingModel([failure])
    assistant = Assistant(engine, model)
    before = snapshot(engine)
    events = assistant.turn("mixed-error", said, client_message_id="mixed-once")
    round3_combined(engine, events, said)
    assert model.seen == []
    assert_changes(before, snapshot(engine), {})
    # Stored events replay without calling the model or recording the demand twice.
    demand = store.demand()
    assert assistant.turn("mixed-error", said, client_message_id="mixed-once") == events
    assert store.demand() == demand and model.seen == []
    assert assistant.turn("mixed-error", "hello")[-1]["code"] == code  # the claim was released


@pytest.mark.parametrize("code", MODEL_ERRORS)
def test_round3_non_mixed_model_errors_keep_their_refusals(engine, code):
    failure = Reply("Partial answer", [], finish="length") if code == "answer_interrupted" else Refusal(
        "The model could not answer.", code)
    model = FailingModel([failure])
    events = Assistant(engine, model).turn("ordinary-error", "hello")
    assert events[-1]["code"] == code
    assert len(model.seen) == 1 and store.demand() == []
    durable_refusal("ordinary-error", events, code)


def round3_question(engine, kind):
    if kind == "setup":
        own_books(engine)
        model = OwnerWords(setup_words)
        assistant = Assistant(engine, model)
        assistant.turn("pending", "hi")
        assistant.turn("pending", "Lakeside Bakery")
        question = assistant.turn("pending", "US dollars")[-1]
        answer = next(c for c in question["choices"] if "January" in c["label"])
    else:
        model = ScriptedModel([Reply("", [invoice("Wayne")]), Reply("", [invoice("Wayne Enterprises")])])
        assistant = Assistant(engine, model)
        question = assistant.turn("pending", "invoice Wayne for 5 copper wire")[-1]
        answer = next(c for c in question["choices"] if c["label"] == "Wayne Enterprises")
    assert question["type"] == "ask" and not answer["control"], question
    return assistant, model, question, answer


@pytest.mark.parametrize("kind", ["customer", "setup"])
@pytest.mark.parametrize("mixed", [True, False])
def test_round3_pending_question_survives_and_can_be_answered(engine, as_released, kind, mixed):
    assistant, model, question, answer = round3_question(engine, kind)
    pending = store.open_request("pending")
    bindings = store.bindings(pending["id"])
    seen = len(model.tools_seen)
    before = snapshot(engine)
    said = MIXED if mixed else "hello"
    events = assistant.turn("pending", said)
    assert events == [question]
    assert store.demand() == []
    assert len(model.tools_seen) == seen
    assert store.open_request("pending") == pending
    assert store.bindings(pending["id"]) == bindings
    assert_changes(before, snapshot(engine), {})
    events = assistant.turn("pending", answer["label"], choice=answer["code"])
    if kind == "customer":
        assert [e["type"] for e in events] == ["card"], events
        assert ["Customer", "Wayne Enterprises"] in events[0]["card"]["lines"]
    else:
        assert events[-1]["type"] == "ask" and events[-1]["text"] == Q_DATES, events


@pytest.mark.parametrize("mixed", [True, False])
def test_round3_pending_card_survives_and_can_be_approved(engine, as_released, mixed):
    model = ScriptedModel([Reply("", [invoice()]), Reply("Okay.", [])])
    assistant = Assistant(engine, model)
    card = assistant.turn("pending-card", "invoice Acme Corporation for 5 copper wire")[-1]
    assert card["type"] == "card"
    pending = store.open_request("pending-card")
    before = snapshot(engine)
    seen = len(model.seen)
    said = MIXED if mixed else "thanks"
    events = assistant.turn("pending-card", said)
    if mixed:
        round3_combined(engine, events, said)
        assert events[-1]["checked"] == "engine"
        assert len(model.seen) == seen + 1
    else:
        assert events[-1]["type"] == "answer" and events[-1]["checked"] == "engine"
    assert store.open_request("pending-card") == pending
    assert_changes(before, snapshot(engine), {})
    assert approvals.pending_card(engine, "pending-card")["id"] == card["approval_id"]
    assert approvals.approve(engine, card["approval_id"], card["digest"]).status == "committed"


@pytest.mark.parametrize("pending_kind", ["card", "question"])
def test_mixed_with_open_request_uses_normal_turn(engine, as_released, monkeypatch, pending_kind):
    if pending_kind == "question":
        assistant, _, question, _ = round3_question(engine, "customer")
        cid = "pending"
    else:
        assistant = Assistant(engine, ScriptedModel([Reply("", [invoice()]), Reply("", [invoice()])]))
        cid = "pending-card"
        card = assistant.turn(cid, "invoice Acme Corporation for 5 copper wire")[-1]
    pending = store.open_request(cid)
    before = snapshot(engine)
    normal_turn = assistant._turn
    routed = []
    created = []
    new_request = store.new_request

    def record_turn(*args, **kwargs):
        routed.append(kwargs.get("mixed"))
        return normal_turn(*args, **kwargs)

    def record_request(*args, **kwargs):
        created.append(True)
        return new_request(*args, **kwargs)

    monkeypatch.setattr(assistant, "_turn", record_turn)
    monkeypatch.setattr(store, "new_request", record_request)
    events = assistant.turn(cid, MIXED)
    assert routed == [True]
    assert created == []
    assert not any(e["type"] == "card" or e.get("code") == "card_set_aside" for e in events)
    assert store.open_request(cid) == pending
    if pending_kind == "question":
        assert events == [question]
    else:
        round3_combined(engine, events, MIXED)
        assert approvals.pending_card(engine, cid)["id"] == card["approval_id"]
    assert_changes(before, snapshot(engine), {})


def test_interrupted_mixed_retry_then_thanks_preserves_pending_card(engine, as_released, monkeypatch):
    assistant = Assistant(engine, ScriptedModel([Reply("", [invoice()]), Reply("Okay.", []),
                                                Reply("Okay.", []), Reply("Okay.", [])]))
    cid = "mixed-retry"
    card = assistant.turn(cid, "invoice Acme Corporation for 5 copper wire")[-1]
    pending = store.open_request(cid)
    before = snapshot(engine)
    finalise = store.finalise
    interrupted = []

    def stop_before_completion(claim, request_id, *args, **kwargs):
        # Interrupt after the request and its message exist. The reconciliation handler releases the claim.
        interrupted.append(store.get_request(request_id))
        raise ReconciliationIncomplete()

    monkeypatch.setattr(store, "finalise", stop_before_completion)
    events = assistant.turn(cid, MIXED, client_message_id="retry-mixed")
    assert events[-1]["code"] == "reconciliation_incomplete"
    assert len(interrupted) == 1 and interrupted[0]["state"] == "open"
    monkeypatch.setattr(store, "finalise", finalise)
    events = assistant.turn(cid, MIXED, client_message_id="retry-mixed")
    round3_combined(engine, events, MIXED)
    demand = store.demand()
    assert assistant.turn(cid, MIXED, client_message_id="retry-mixed") == events
    assert store.demand() == demand
    events = assistant.turn(cid, "thanks")
    assert not any(e.get("code") == "card_set_aside" for e in events)
    assert store.open_request(cid) == pending
    with store._tx() as conn:
        requests = conn.execute("SELECT id FROM request WHERE conversation_id = ? AND state = 'open'",
                                (cid,)).fetchall()
    assert [row["id"] for row in requests] == [pending["id"]]
    assert approvals.pending_card(engine, cid)["id"] == card["approval_id"]
    assert_changes(before, snapshot(engine), {})
    assert approvals.approve(engine, card["approval_id"], card["digest"]).status == "committed"


@pytest.mark.parametrize("kind", ["customer", "setup"])
@pytest.mark.parametrize("mixed", [True, False])
def test_round3_explicit_choice_keeps_the_choice_path(engine, as_released, kind, mixed):
    assistant, model, question, answer = round3_question(engine, kind)
    pending = store.open_request("pending")
    events = assistant.turn("pending", MIXED if mixed else answer["label"], choice=answer["code"])
    assert store.demand() == []
    message = store.messages(pending["id"])[-1]
    assert message["answer"] == 1
    assert store.message_resolution(message["id"])["question_id"] == question["question_id"]
    if mixed:
        # Main consumes the choice, then its existing write-intent check asks about the accompanying text.
        assert [e["type"] for e in events] == ["ask"], events
        assert events[0]["code"] == "changes_ambiguous" and events[0]["text"] == ONE_CHANGE
    elif kind == "customer":
        assert [e["type"] for e in events] == ["card"], events
        assert ["Customer", "Wayne Enterprises"] in events[0]["card"]["lines"]
    else:
        assert events[-1]["type"] == "ask" and events[-1]["text"] == Q_DATES, events


@pytest.mark.parametrize("mixed", [True, False])
@pytest.mark.parametrize("guard", ["credential", "books"])
def test_round3_hosted_guards_win_before_mixed_detection(engine, guard, mixed):
    fake = FakeServer()
    assistant = Assistant(engine, openrouter(fake))
    said = MIXED if mixed else "hello"
    if guard == "credential":
        credential = _shapes()["openrouter_key"]
        said += " " + credential
    else:
        unmark(engine, DEMO_MARK, EVAL_MARK)
    before = snapshot(engine)
    events = assistant.turn("guarded", said)
    assert events[-1]["code"] == ("credential_refused" if guard == "credential" else "hosted_books_refused")
    assert fake.bodies == [] and store.demand() == []
    assert store.unassigned("guarded") == [] and store.open_request("guarded") is None
    if guard == "credential":
        assert not _stored(credential)
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("mixed", [True, False])
def test_round3_live_claim_still_refuses_the_message(engine, mixed):
    assistant = Assistant(engine, ScriptedModel([]))
    from erpclaw_core import principals
    with engine.connect() as conn:
        owner = principals.owner(conn)
    claim = store.claim("busy", "first", principal_id=owner["id"], principal_name=owner["name"])
    events = assistant.turn("busy", MIXED if mixed else "hello")
    assert events[-1]["code"] == "busy" and store.demand() == []
    assert assistant.model.seen == []
    store.release(claim)
