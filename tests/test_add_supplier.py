"""Supplier cards, owner choices and the exact effect of an approved master write."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from bookdiff import assert_changes, snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall, call
from erpclaw_core import administration as admin, approvals, catalogue as cat, masters, models as m
from erpclaw_core import principals as pr
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_core.setup import create_company


def supplier_contract():
    return REGISTRY.contracts["add_supplier"]


def propose(engine, args, **kwargs):
    return approvals.propose(engine, supplier_contract(), args, **kwargs)


def tool(args):
    return ToolCall("call_add_supplier", "add_supplier", args, json.dumps(args))


def chat_client(engine, args, prefix=()):
    supplier_contract()
    model = ScriptedModel([*prefix, *[Reply("", [tool(args)]) for _ in range(4)]])
    client = TestClient(create_app(engine, model, embedded_owner=True))
    cid = client.post("/v1/conversations").json()["id"]
    return client, cid


def message(client, cid, text="", choice=None):
    body = {"text": text} if choice is None else {"text": choice["label"], "choice": choice["code"]}
    response = client.post(f"/v1/conversations/{cid}/messages", json=body)
    assert response.status_code == 200, response.text
    return response.json()["events"]


def approve(client, card):
    aid = card["approval_id"] if isinstance(card, dict) else card.approval_id
    digest = card["digest"] if isinstance(card, dict) else card.digest
    response = client.post(f"/v1/cards/{aid}/approve", json={"digest": digest})
    assert response.status_code == 200, response.text
    return response.json()["events"]


@pytest.fixture
def copperline(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        return masters.create_party(ctx, "supplier", "Copperline Metals", city="Leeds")


@pytest.mark.parametrize("name", ["Copperline Metals", "  COPPERLINE   metals  "])
def test_exact_duplicate_refused_remove_same_name_check(engine, copperline, name):
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, {"name": name, "new_supplier": True})
    assert err.value.code == "supplier_exists"
    assert err.value.message == "Copperline Metals is already a supplier."
    assert_changes(before, snapshot(engine), {})


def near_question(engine):
    client, cid = chat_client(engine, {"name": "Copperline Metal"})
    before = snapshot(engine)
    [ask] = message(client, cid, "Add a supplier called Copperline Metal")
    assert ask["type"] == "ask" and ask["code"] == "supplier_near_ambiguous", ask
    assert ask["text"] == ("Did you mean Copperline Metals (Leeds), already a supplier, "
                           "or is Copperline Metal a new one?")
    assert [c["label"] for c in ask["choices"] if not c["control"]] == [
        "Copperline Metals (Leeds), already a supplier", "Copperline Metal, a new supplier"]
    assert_changes(before, snapshot(engine), {})
    return client, cid, ask


def test_near_name_asks_two_answers_writes_nothing_remove_near_branch(engine, copperline):
    near_question(engine)


def test_existing_near_answer_refused_remove_false_answer_branch(engine, copperline):
    client, cid, ask = near_question(engine)
    before = snapshot(engine)
    existing = next(c for c in ask["choices"] if not c["control"] and "already" in c["label"])
    events = message(client, cid, choice=existing)
    assert not any(e["type"] == "card" for e in events), events
    assert events[-1]["code"] == "supplier_exists", events
    assert_changes(before, snapshot(engine), {})


def test_new_near_answer_binds_card_remove_answer_or_warning(engine, copperline):
    client, cid, ask = near_question(engine)
    new = next(c for c in ask["choices"] if not c["control"] and "a new supplier" in c["label"])
    before = snapshot(engine)
    [card] = message(client, cid, choice=new)
    assert card["type"] == "card" and card["card"]["title"] == "Add a supplier", card
    assert card["card"]["warnings"] == [
        "Copperline Metals is already a supplier; this adds Copperline Metal as another one."]
    after = snapshot(engine)
    assert json.loads(after["approval"][(card["approval_id"],)]["args_json"])["new_supplier"] is True
    assert_changes(before, after, {"approval": {"added": [{"id": card["approval_id"], "status": "pending"}]}})
    assert approve(client, card)[-1]["status"] == "committed"
    with engine.connect() as conn:
        names = set(conn.execute(select(m.party.c.name).where(m.party.c.kind == "supplier")).scalars())
    assert {"Copperline Metal", "Copperline Metals"} <= names


def test_ambiguous_supplier_lists_options_remove_match_ambiguity(engine, copperline):
    supplier_contract()
    with engine.begin() as conn:
        masters.create_party(company_ctx(conn), "supplier", "Copperline Tools")
    before = snapshot(engine)
    with engine.connect() as conn, pytest.raises(Refusal) as err:
        masters.find_supplier(company_ctx(conn), "Copperline")
    assert err.value.code == "supplier_ambiguous"
    assert set(err.value.options) == {"Copperline Metals", "Copperline Tools"}
    assert all(name in err.value.message for name in err.value.options)
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("args", [{}, {"name": ""}, {"name": "   "}, {"name": '"   "'}])
def test_missing_name_refused_remove_required_name_guard(engine, args):
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, args)
    assert err.value.code == "supplier_missing" and err.value.message == "Which supplier?"
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("email", ["accounts", "a b@example.com", "a@example", "a@@example.com"])
def test_bad_email_refused_remove_address_validator(engine, email):
    before = snapshot(engine)
    with pytest.raises(ValidationError, match="an email address is like name@example.com"):
        propose(engine, {"name": "Lakeside Parts", "email": email})
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("field", ["id", "party_id", "supplier_id"])
@pytest.mark.parametrize("kind", ["supplier", "customer"])
@pytest.mark.parametrize("contact_given", [False, True])
def test_assistant_drops_model_id_and_ungrounded_fields_remove_owner_word_filter(
        engine, monkeypatch, field, kind, contact_given):
    contract = REGISTRY.contracts[f"add_{kind}"]
    args = {"name": "Lakeside Parts", "email": "orders@lakeside.example", "city": "York",
            field: "model-picked-id"}
    validated = []
    original = contract.input_model.model_validate

    def validate(cls, data, *positional, **kwargs):
        validated.append(dict(data))
        return original(data, *positional, **kwargs)

    monkeypatch.setattr(contract.input_model, "model_validate", classmethod(validate))
    model = ScriptedModel([Reply("", [ToolCall("add_party", contract.name, args, json.dumps(args))])])
    client = TestClient(create_app(engine, model, embedded_owner=True))
    cid = client.post("/v1/conversations").json()["id"]
    before = snapshot(engine)
    owner_words = f"Add a {kind} called Lakeside Parts"
    if contact_given:
        owner_words += ", orders@lakeside.example, York"
    [card] = message(client, cid, owner_words)
    assert card["type"] == "card" and card["card"]["title"] == f"Add a {kind}", card
    assert validated
    for data in validated:
        assert not {"id", "party_id", "supplier_id"} & data.keys(), data
        assert data["name"] == "Lakeside Parts"
        assert data.get("email") == (args["email"] if contact_given else None)
        assert data.get("city") == (args["city"] if contact_given else None)
    assert [line for line in card["card"]["lines"] if line[0] in {"Name", "Email", "City"}] == [
        ["Name", "Lakeside Parts"],
        ["Email", args["email"] if contact_given else "none given"],
        ["City", args["city"] if contact_given else "none given"]]
    assert "model-picked-id" not in json.dumps(card)
    after = snapshot(engine)
    stored_args = json.loads(after["approval"][(card["approval_id"],)]["args_json"])
    assert stored_args == {"name": "Lakeside Parts", "email": args["email"] if contact_given else None,
                           "city": args["city"] if contact_given else None, f"new_{kind}": None}
    assert_changes(before, after, {"approval": {"added": [{"id": card["approval_id"],
                                                         "contract": contract.name}]}})


def test_digest_approval_exact_book_diff_remove_party_or_either_audit(engine):
    Assistant(engine, ScriptedModel([]))
    args = {"name": "Lakeside Parts", "email": "orders@lakeside.example", "city": "York"}
    before_card = snapshot(engine)
    card = propose(engine, args, date_source={"not_applicable": True})
    before = snapshot(engine)
    stored = before["approval"][(card.approval_id,)]
    cid = stored["company_id"]
    assert card.card["title"] == "Add a supplier"
    assert all([key.title(), value] in card.card["lines"] for key, value in args.items())
    assert card.card["books"] == ["Nothing in the books changes."]
    assert ["Date source", "No date applies: adding a supplier posts nothing."] in card.card["lines"]
    assert card.card["footer"].endswith("Valid 10 minutes. Rechecked before saving.")
    rs = json.loads(stored["read_set_json"])
    assert len(rs) == 1 and rs[0][:2] == ["company", cid] and len(rs[0][2]) == 64
    assert len(card.digest) == 64 and card.operation_key and card.operation_key != card.approval_id
    assert_changes(before_card, before, {"approval": {"added": [{"id": card.approval_id, "status": "pending",
                                                                 "operation_key": card.operation_key}]}})
    client = TestClient(create_app(engine, ScriptedModel([]), embedded_owner=True))
    [event] = approve(client, card)
    assert event["status"] == "committed", event
    after = snapshot(engine)
    row = after["approval"][(card.approval_id,)]
    [party] = [r for k, r in after["party"].items() if k not in before["party"]]
    assert_changes(before, after, {
        "party": {"added": [{"id": party["id"], "company_id": cid, "kind": "supplier", **args,
                              "city_key": "york"}]},
        "audit_log": {"added": [
            {"company_id": cid, "actor": "owner", "action": "add_supplier", "document_type": "party",
             "document_id": party["id"], "approval_id": None, "detail": json.dumps(args, sort_keys=True)},
            {"company_id": cid, "actor": "owner", "action": "commit:add_supplier", "document_type": None,
             "document_id": None, "approval_id": card.approval_id,
             "detail": json.dumps({"name": args["name"], "operation_key": card.operation_key}, sort_keys=True)}]},
        "approval": {"changed": [({"id": card.approval_id}, {
            "status": "consumed", "decided_at": row["decided_at"], "approved_by": stored["principal_id"],
            "approver_grants_version": stored["policy_version"]})]},
        "operation_outcome": {"added": [{"key": card.operation_key, "company_id": cid,
                                         "approval_id": card.approval_id, "card_digest": card.digest,
                                         "status": "committed", "result_json": approvals.canonical({
                                             "text": "Added Lakeside Parts as a supplier.",
                                             "data": {"name": "Lakeside Parts"}, "figures": [], "amounts": []})}]}})
    assert row["decided_at"] is not None


def test_replay_returns_stored_result_writes_nothing_remove_outcome_replay(engine, monkeypatch):
    card = propose(engine, {"name": "Lakeside Parts"})
    client = TestClient(create_app(engine, ScriptedModel([]), embedded_owner=True))
    first = approve(client, card)
    assert first[-1]["status"] == "committed"
    before = snapshot(engine)

    def cannot_commit(*args):
        pytest.fail("A consumed card must return its stored outcome without re-running commit")

    monkeypatch.setattr(supplier_contract(), "commit", cannot_commit)
    assert approve(client, card) == first
    assert approvals.read_back(engine, card.operation_key).data == {"name": "Lakeside Parts"}
    assert_changes(before, snapshot(engine), {})


def test_consumed_card_rejects_another_digest_remove_digest_check(engine):
    card = propose(engine, {"name": "Lakeside Parts"})
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        approvals.approve(engine, card.approval_id, "0" * 64)
    assert err.value.code == "digest"
    assert_changes(before, snapshot(engine), {})


def principal(engine, company_id, name, permissions):
    with engine.begin() as conn:
        token = admin.add_principal(conn, name, permissions, [company_id])
        return pr.authenticate(conn, token), token


def test_draft_only_cannot_approve_own_card_remove_approve_grant_check(engine):
    with engine.connect() as conn:
        cid = company_ctx(conn).company_id
    drafter, token = principal(engine, cid, "drafter", ["view", "draft"])
    card = propose(engine, {"name": "Lakeside Parts"}, principal=drafter)
    client = TestClient(create_app(engine, ScriptedModel([])))
    before = snapshot(engine)
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403 and "approve" in response.text, response.text
    assert_changes(before, snapshot(engine), {})


def test_cross_company_approval_refused_remove_company_grant_scope(engine):
    with engine.connect() as conn:
        ours = company_ctx(conn).company_id
    create_company(engine, "Other Co")
    with engine.connect() as conn:
        other = company_ctx(conn, company_name="Other Co").company_id
    _, token = principal(engine, other, "other approver", ["view", "draft", "approve", "post"])
    card = propose(engine, {"name": "Lakeside Parts"}, company_id=ours)
    client = TestClient(create_app(engine, ScriptedModel([])))
    before = snapshot(engine)
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403, response.text
    assert_changes(before, snapshot(engine), {})


def test_catalogue_resolves_add_supplier_remove_registration_or_synonyms(engine, as_released):
    contract = supplier_contract()
    assert contract.plugin == "core" and contract.effect == "write" and contract.moves_money is False
    props = contract.tool_schema()["function"]["parameters"]
    assert set(props["properties"]) == {"name", "email", "city"} and props["required"] == ["name"]
    with engine.connect() as conn:
        found = cat.find_capability(conn, "add a supplier")
        assert found[0].id == "op.add_supplier" and found[0].state == "installed"
        decision = cat.decide(found, "op.add_supplier", named=cat.named_by_synonym(conn, "add a supplier"))
    assert decision.outcome == "a" and decision.entry.target == "add_supplier"
    prefix = [Reply("", [call("find_capability", request="add a supplier")]),
              Reply("", [call("choose_capability", pick="op.add_supplier")])]
    client, cid = chat_client(engine, {"name": "Lakeside Parts"}, prefix)
    before = snapshot(engine)
    [card] = message(client, cid, "Add a supplier called Lakeside Parts")
    assert card["type"] == "card" and card["card"]["title"] == "Add a supplier", card
    assert_changes(before, snapshot(engine), {"approval": {"added": [{"id": card["approval_id"],
                                                                     "contract": "add_supplier"}]}})


def test_commit_rechecks_duplicate_remove_commit_plan(engine):
    contract = supplier_contract()
    card = propose(engine, {"name": "Lakeside Parts"})
    with engine.begin() as conn:
        masters.create_party(company_ctx(conn), "supplier", "Lakeside Parts")
    # Call the commit entry directly in a rolled-back transaction: a duplicate must refuse before inserting.
    before = snapshot(engine)
    with engine.begin() as conn, pytest.raises(Refusal) as err:
        contract.commit(company_ctx(conn), contract.input_model(name="Lakeside Parts"), card.operation_key)
    assert err.value.code == "supplier_exists"
    assert_changes(before, snapshot(engine), {})
