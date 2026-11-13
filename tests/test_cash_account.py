"""Cash lookup and an approved chart addition over PostgreSQL books."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from bookdiff import assert_changes, snapshot
from conftest import drop_everything, empty_books
from erpclaw_api.app import create_app
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import administration as admin, approvals, catalogue as cat, ledger, models as m
from erpclaw_core import principals as pr
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.plugins import REGISTRY
from erpclaw_core.setup import create_company


@pytest.fixture
def engine():
    eng = empty_books()
    create_company(eng, "Cash Books")
    Assistant(eng, ScriptedModel([]))
    yield eng
    drop_everything(eng)
    eng.dispose()


def contract():
    return REGISTRY.contracts["add_cash_account"]


def propose(engine, **kw):
    return approvals.propose(engine, contract(), {}, **kw)


def add_account(engine, number="1110", name="Cash on Hand", **kw):
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_name="Cash Books")
        aid = new_id()
        parent = ledger.account_by_number(ctx, "1000")["id"]
        values = dict(id=aid, company_id=ctx.company_id, number=number, name=name, root_type="asset",
                      account_type="bank", normal_side="debit", parent_id=parent, is_group=False, frozen=False)
        values.update(kw)
        conn.execute(m.account.insert().values(**values))
    return aid


def lookup(engine, table_order=False):
    with engine.connect() as conn:
        if table_order:
            for scan in ("indexscan", "indexonlyscan", "bitmapscan"):
                conn.execute(text(f"SET LOCAL enable_{scan} = off"))
        return ledger.cash_account(company_ctx(conn, company_name="Cash Books"))


def refused_lookup(engine, code, table_order=False):
    with pytest.raises(Refusal) as err:
        lookup(engine, table_order)
    assert err.value.code == code
    return err.value


def test_fresh_books_refuse_cash_replace_helper_with_bank_lookup(engine):
    with engine.connect() as conn:
        assert ledger.account_by_type(company_ctx(conn, company_name="Cash Books"), "bank")["number"] == "1100"
    before = snapshot(engine)
    refused_lookup(engine, "no_cash_account")
    assert_changes(before, snapshot(engine), {})


def test_one_live_cash_binds_remove_live_return(engine):
    aid = add_account(engine)
    assert lookup(engine)["id"] == aid


def test_two_live_cash_refuse_with_both_names_choose_first_live(engine):
    add_account(engine, "1130")
    add_account(engine, "1110", " cash  ON hand ")
    r = refused_lookup(engine, "several_cash_accounts")
    assert "1110  cash  ON hand " in r.message and "1130 Cash on Hand" in r.message
    assert r.message.index("1110") < r.message.index("1130")


def test_frozen_only_names_lowest_number_remove_order_by(engine):
    add_account(engine, "1130", frozen=True)
    add_account(engine, "1110", " cash  ON hand ", frozen=True)
    r = refused_lookup(engine, "frozen_account", table_order=True)
    assert r.message == "1110  cash  ON hand  is frozen."


def test_live_cash_wins_over_frozen_refuse_any_frozen(engine):
    add_account(engine, "1110", frozen=True)
    aid = add_account(engine, "1120")
    assert lookup(engine)["id"] == aid


def test_name_normalisation_ignores_type_and_number_match_bank_role(engine):
    add_account(engine, "1110", "Petty Cash")
    refused_lookup(engine, "no_cash_account")
    aid = add_account(engine, "8900", " cash  ON hand ", account_type=None)
    assert lookup(engine)["id"] == aid


def test_groups_and_other_companies_do_not_bind_remove_candidate_scope(engine):
    add_account(engine, is_group=True)
    other = create_company(engine, "Other Co")
    add_account(engine, "1120", company_id=other, parent_id=None)
    refused_lookup(engine, "no_cash_account")


def test_digest_approval_exact_book_diff_remove_insert_or_either_audit(engine):
    before_card = snapshot(engine)
    card = propose(engine, date_source={"not_applicable": True})
    before = snapshot(engine)
    stored = before["approval"][(card.approval_id,)]
    cid = stored["company_id"]
    parent = next(r["id"] for r in before["account"].values() if r["number"] == "1000")
    assert card.card["title"] == "Add Cash on Hand"
    assert ["Account", "1110 Cash on Hand (cash you hold)"] in card.card["lines"]
    assert ["Parent", "1000 Assets"] in card.card["lines"]
    assert card.card["books"] == [
        "Nothing in the books changes; this only adds the place the money will be paid from."]
    assert ["Date source", "No date applies: adding an account posts nothing."] in card.card["lines"]
    assert card.card["footer"].endswith("Valid 10 minutes. Rechecked before saving.")
    rs = json.loads(stored["read_set_json"])
    assert {(t, k) for t, k, _ in rs} == {("company", cid)} | {
        ("account", r["id"]) for r in before["account"].values()}
    assert all(len(d) == 64 for _, _, d in rs)
    assert json.loads(stored["args_json"]) == {}
    assert len(card.digest) == 64 and card.operation_key != card.approval_id
    assert_changes(before_card, before, {"approval": {"added": [{"id": card.approval_id, "status": "pending",
                                                                 "contract": "add_cash_account"}]}})
    client = TestClient(create_app(engine, ScriptedModel([]), embedded_owner=True))
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest})
    assert response.status_code == 200, response.text
    [event] = response.json()["events"]
    assert event["status"] == "committed", event
    account = lookup(engine)  # removing the insert must still refuse here
    after = snapshot(engine)
    row = after["approval"][(card.approval_id,)]
    data = {"name": "Cash on Hand", "number": "1110"}
    assert_changes(before, after, {
        "account": {"added": [{"id": account["id"], "company_id": cid, "number": "1110", "name": "Cash on Hand",
                                "root_type": "asset", "account_type": "bank", "parent_id": parent,
                                "normal_side": "debit", "is_group": False, "frozen": False}]},
        "audit_log": {"added": [
            {"company_id": cid, "actor": "owner", "action": "add_cash_account", "document_type": "account",
             "document_id": account["id"], "approval_id": None, "detail": json.dumps(data, sort_keys=True)},
            {"company_id": cid, "actor": "owner", "action": "commit:add_cash_account", "document_type": None,
             "document_id": None, "approval_id": card.approval_id,
             "detail": json.dumps({**data, "operation_key": card.operation_key}, sort_keys=True)}]},
        "approval": {"changed": [({"id": card.approval_id}, {
            "status": "consumed", "decided_at": row["decided_at"], "approved_by": stored["principal_id"],
            "approver_grants_version": stored["policy_version"]})]},
        "operation_outcome": {"added": [{"key": card.operation_key, "company_id": cid,
                                         "approval_id": card.approval_id, "card_digest": card.digest,
                                         "status": "committed", "result_json": approvals.canonical({
                                             "text": "Added 1110 Cash on Hand.", "data": data,
                                             "figures": [], "amounts": []})}]}})
    assert after["ledger_entry"] == {} and after["posting"] == {} and after["number_series"] == {}
    assert row["decided_at"] is not None


@pytest.mark.parametrize("occupied, expected", [(["1110"], "1120"), (["1110", "1120", "1140"], "1130")])
def test_next_free_number_use_fixed_1110_or_skip_gap(engine, occupied, expected):
    for number in occupied:
        add_account(engine, number, "Savings")
    card = propose(engine)
    assert ["Account", f"{expected} Cash on Hand (cash you hold)"] in card.card["lines"]
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    assert lookup(engine)["number"] == expected


@pytest.mark.parametrize("frozen, code", [(False, "cash_account_exists"), (True, "frozen_account")])
def test_existing_cash_refuses_proposal_remove_duplicate_plan(engine, frozen, code):
    add_account(engine, name=" cash  ON hand ", frozen=frozen)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine)
    assert err.value.code == code and "1110  cash  ON hand " in err.value.message
    assert_changes(before, snapshot(engine), {})


def test_several_existing_cash_refuses_proposal_hide_helper_ambiguity(engine):
    add_account(engine, "1130")
    add_account(engine, "1110")
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine)
    assert err.value.code == "several_cash_accounts"
    assert "1110 Cash on Hand" in err.value.message and "1130 Cash on Hand" in err.value.message
    assert_changes(before, snapshot(engine), {})


def test_replay_returns_stored_result_remove_outcome_replay(engine, monkeypatch):
    card = propose(engine)
    first = approvals.approve(engine, card.approval_id, card.digest)
    assert first.status == "committed"
    before = snapshot(engine)

    def cannot_commit(*args):
        pytest.fail("A consumed card must replay without running commit")

    monkeypatch.setattr(contract(), "commit", cannot_commit)
    again = approvals.approve(engine, card.approval_id, card.digest)
    assert (again.status, again.text, again.data) == (first.status, first.text, first.data)
    assert approvals.read_back(engine, card.operation_key).data == {"name": "Cash on Hand", "number": "1110"}
    assert_changes(before, snapshot(engine), {})


def test_consumed_card_rejects_another_digest_remove_digest_check(engine):
    card = propose(engine)
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        approvals.approve(engine, card.approval_id, "0" * 64)
    assert err.value.code == "digest"
    assert_changes(before, snapshot(engine), {})


def principal(engine, cid, name, permissions):
    with engine.begin() as conn:
        token = admin.add_principal(conn, name, permissions, [cid])
        return pr.authenticate(conn, token), token


def test_draft_only_cannot_approve_own_card_remove_approve_grant_check(engine):
    with engine.connect() as conn:
        cid = company_ctx(conn, company_name="Cash Books").company_id
    drafter, token = principal(engine, cid, "drafter", ["view", "draft"])
    card = propose(engine, principal=drafter)
    client = TestClient(create_app(engine, ScriptedModel([])))
    before = snapshot(engine)
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403 and "approve" in response.text, response.text
    assert_changes(before, snapshot(engine), {})


def test_cross_company_approval_refused_remove_company_grant_scope(engine):
    card = propose(engine)
    other = create_company(engine, "Other Co")
    _, token = principal(engine, other, "other approver", ["view", "draft", "approve", "post"])
    client = TestClient(create_app(engine, ScriptedModel([])))
    before = snapshot(engine)
    response = client.post(f"/v1/cards/{card.approval_id}/approve", json={"digest": card.digest},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403, response.text
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("frozen", [False, True])
def test_racing_cash_insert_supersedes_remove_cash_name_guard(engine, frozen):
    card = propose(engine)
    add_account(engine, "1120", " cash  ON hand ", frozen=frozen)
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "superseded" and out.data["code"] == (
        "frozen_account" if frozen else "cash_account_exists")
    after = snapshot(engine)
    ended = after["approval"][(card.approval_id,)]
    assert_changes(before, after, {"approval": {"changed": [({"id": card.approval_id}, {
        "status": "superseded", "decided_at": ended["decided_at"], "decision_json": ended["decision_json"]})]}})
    again = approvals.approve(engine, card.approval_id, card.digest)
    assert (again.status, again.text, again.data) == (out.status, out.text, out.data)
    assert_changes(after, snapshot(engine), {})


def test_racing_number_insert_supersedes_reuse_stored_number(engine):
    card = propose(engine)
    add_account(engine, name="Savings")
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "superseded" and out.approval_id != card.approval_id
    assert ["Account", "1120 Cash on Hand (cash you hold)"] in out.card["lines"]
    after = snapshot(engine)
    ended = after["approval"][(card.approval_id,)]
    assert_changes(before, after, {"approval": {
        "added": [{"id": out.approval_id, "status": "pending", "contract": "add_cash_account"}],
        "changed": [({"id": card.approval_id}, {"status": "superseded", "decided_at": ended["decided_at"],
                                                  "decision_json": ended["decision_json"]})]}})
    refused_lookup(engine, "no_cash_account")


def test_commit_rechecks_plan_remove_commit_plan(engine):
    card = propose(engine)
    add_account(engine)
    before = snapshot(engine)
    with engine.begin() as conn, pytest.raises(Refusal) as err:
        contract().commit(company_ctx(conn, company_name="Cash Books"), contract().input_model(), card.operation_key)
    assert err.value.code == "cash_account_exists"
    assert_changes(before, snapshot(engine), {})


def test_two_bank_accounts_still_refuse_choose_first_bank(engine):
    card = propose(engine)
    assert approvals.approve(engine, card.approval_id, card.digest).status == "committed"
    with engine.connect() as conn, pytest.raises(Refusal) as err:
        ledger.account_by_type(company_ctx(conn, company_name="Cash Books"), "bank")
    assert err.value.code == "several_bank_accounts"


def test_catalogue_and_empty_input_remove_registration_or_add_model_account_fields(engine, as_released):
    c = contract()
    assert c.plugin == "core" and c.effect == "write" and c.moves_money is False
    assert c.tool_schema()["function"]["parameters"]["properties"] == {}
    for field in ("id", "account_id", "number", "amount", "date"):
        with pytest.raises(ValidationError):
            c.input_model.model_validate({field: "model supplied"})
    with engine.connect() as conn:
        assert c.input_model().takes_no_date(company_ctx(conn, company_name="Cash Books")) is True
        found = cat.find_capability(conn, "add a cash account")
        decision = cat.decide(found, "op.add_cash_account", named=cat.named_by_synonym(conn, "add a cash account"))
    assert decision.outcome == "a" and decision.entry.target == "add_cash_account"
    model = ScriptedModel([Reply("", [call("find_capability", request="add a cash account")]),
                           Reply("", [call("choose_capability", pick="op.add_cash_account")]),
                           Reply("", [call("add_cash_account")])])
    client = TestClient(create_app(engine, model, embedded_owner=True))
    cid = client.post("/v1/conversations").json()["id"]
    before = snapshot(engine)
    [card] = client.post(f"/v1/conversations/{cid}/messages", json={"text": "Add a cash account"}).json()["events"]
    assert card["type"] == "card" and card["card"]["title"] == "Add Cash on Hand", card
    assert_changes(before, snapshot(engine), {"approval": {"added": [{"id": card["approval_id"],
                                                                     "contract": "add_cash_account"}]}})
