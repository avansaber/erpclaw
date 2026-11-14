"""Turn steps survive the API and stored-message replay unchanged."""
import json

import pytest
from fastapi.testclient import TestClient

from erpclaw_api.app import create_app
from erpclaw_assistant import store
from erpclaw_assistant.model import Reply, ScriptedModel, call


@pytest.mark.parametrize("kind", ["answer", "card"])
def test_turn_steps_pass_through_and_replay_from_storage(engine, monkeypatch, kind):
    tool = (call("list_invoices", status="open") if kind == "answer" else
            call("create_invoice", customer="Acme Corporation",
                 items=[{"item": "copper wire", "quantity": 1}]))
    model = ScriptedModel([Reply("", [tool])])
    app = create_app(engine, model, embedded_owner=True)
    original = app.state.assistant.turn
    finalise = store.finalise
    request_ids = []

    def capture_finalise(claim, request_id, events, **kwargs):
        request_ids.append(request_id)
        return finalise(claim, request_id, events, **kwargs)

    monkeypatch.setattr(store, "finalise", capture_finalise)
    produced = []

    def capture(*args, **kwargs):
        events = original(*args, **kwargs)
        produced.append(events)
        return events

    monkeypatch.setattr(app.state.assistant, "turn", capture)
    client = TestClient(app)
    cid = client.post("/v1/conversations", json={}).json()["id"]
    path = f"/v1/conversations/{cid}/messages"
    body = {"text": "Check the books" if kind == "answer" else
            "Invoice Acme Corporation for one copper wire", "client_message_id": "a1b2c3d4"}
    response = client.post(path, json=body)
    assert response.status_code == 200
    event = next(e for e in response.json()["events"] if e["type"] == kind)
    engine_event = next(e for e in produced[0] if e["type"] == kind)
    steps = event["steps"]
    assert steps == engine_event["steps"] and 1 <= len(steps) <= 5
    assert [step["n"] for step in steps] == list(range(1, len(steps) + 1))
    saved = store.events(request_ids[0])
    saved_steps = next(e["steps"] for e in saved if e["type"] == kind)
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert compact(steps) == compact(saved_steps)
    replay = client.post(path, json=body)
    assert replay.status_code == 200 and replay.content == response.content
    assert compact(next(e["steps"] for e in replay.json()["events"] if e["type"] == kind)) == compact(saved_steps)
    assert len(model.seen) == 1


@pytest.mark.parametrize("action", ["approve", "decline"])
def test_card_action_result_has_no_steps(engine, action):
    model = ScriptedModel([Reply("", [call("create_invoice", customer="Acme Corporation",
                                              items=[{"item": "copper wire", "quantity": 1}])])])
    client = TestClient(create_app(engine, model, embedded_owner=True))
    cid = client.post("/v1/conversations", json={}).json()["id"]
    response = client.post(f"/v1/conversations/{cid}/messages",
                           json={"text": "Invoice Acme Corporation for one copper wire"})
    assert response.status_code == 200
    card = next(e for e in response.json()["events"] if e["type"] == "card")
    response = client.post(f"/v1/cards/{card['approval_id']}/{action}",
                           json={"digest": card["digest"]} if action == "approve" else {})
    assert response.status_code == 200
    result = next(e for e in response.json()["events"] if e["type"] == "result")
    assert "steps" not in result
