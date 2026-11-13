"""A model's read call cannot turn a card correction into a list."""
import json
from pathlib import Path

import pytest

from conftest import PG_URL
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_cli.evals.run import EvalBooks
from erpclaw_cli.evals.seeds import services_co


@pytest.fixture(scope="module")
def books():
    b = EvalBooks(PG_URL)
    try:
        b.seed(services_co)
        yield b
    finally:
        b.close()


@pytest.fixture
def trial(books, monkeypatch):
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-11-03")
    eng = books.trial()
    yield eng
    books.finish(eng)


def test_public_correction_with_list_call(trial):
    case = json.loads(Path("evals/honesty/h-007.json").read_text())
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    assert a.turn("regression", case["turns"][0])[-1]["type"] == "card"
    events = a.turn("regression", case["turns"][1])
    assert events[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(events[-1]), events


@pytest.mark.parametrize("message", ["no, Wayne, not Acme", "not Acme, Wayne", "I meant Wayne",
                                     "what I meant was Wayne", "Wayne, not Acme", "actually Wayne",
                                     "so it should be Wayne", "I said Wayne, not Acme", "what customer I meant was Wayne"])
@pytest.mark.parametrize("as_list", [True, False])
def test_correction_has_same_resolution_for_list_and_text(trial, message, as_list):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    reply = Reply("", [call("list_customers")]) if as_list else Reply("Got it.")
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), reply]))
    assert a.turn("correction", "Invoice Acme Corporation for one website audit.")[-1]["type"] == "card"
    events = a.turn("correction", message)
    assert events[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(events[-1]), events
    assert sum(e["type"] == "ask" for e in events) <= 1


@pytest.mark.parametrize("message", ["what customers do we have?", "show me the customers", "who are our customers?",
                                     "well, show me the customers"])
def test_explicit_customer_list_still_lists(trial, message):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    a.turn("listing", "Invoice Acme Corporation for one website audit.")
    events = a.turn("listing", message)
    assert events[-1]["type"] == "answer" and "Acme Corporation" in events[-1]["text"], events


@pytest.mark.parametrize("message", ["I meant Zzyzx", "I meant Traders"])
def test_unknown_or_ambiguous_correction_asks_once(trial, message):
    from erpclaw_core.common import company_ctx
    from erpclaw_core.masters import create_party

    with trial.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "customer", "North Traders", "north@example.invalid", "York")
        create_party(ctx, "customer", "South Traders", "south@example.invalid", "Leeds")
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    a.turn("unknown", "Invoice Acme Corporation for one website audit.")
    events = a.turn("unknown", message)
    assert [e["type"] for e in events].count("ask") == 1 and events[-1]["type"] == "ask", events


@pytest.mark.parametrize("model_call", [call("get_metric", metric="receivables"),
                                         call("create_invoice", customer="Hooli", items=[{"item": "Website audit", "quantity": 1}])])
def test_other_model_calls_cannot_override_a_named_correction(trial, model_call):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [model_call])]))
    a.turn("other", "Invoice Acme Corporation for one website audit.")
    events = a.turn("other", "I meant Wayne")
    assert events[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(events[-1]), events


def test_correction_after_card_was_set_aside(trial):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("Okay."), Reply("", [call("list_customers")])]))
    a.turn("aside", "Invoice Acme Corporation for one website audit.")
    assert a.turn("aside", "cancel that")[-1]["type"] == "notice"
    events = a.turn("aside", "I meant Wayne")
    assert events[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(events[-1]), events


@pytest.mark.parametrize("model_call", [call("get_metric", metric="receivables"),
                                         call("create_invoice", customer="Hooli", items=[{"item": "Website audit", "quantity": 1}]),
                                         call("create_invoice", customer="Zzyzx", items=[{"item": "Website audit", "quantity": 1}])])
def test_unknown_correction_with_another_model_call_asks(trial, model_call):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [model_call])]))
    a.turn("unknown_other", "Invoice Acme Corporation for one website audit.")
    events = a.turn("unknown_other", "I meant Zzyzx")
    assert [e["type"] for e in events].count("ask") == 1 and events[-1]["type"] == "ask", events


@pytest.mark.parametrize("read_first", [True, False])
def test_read_in_same_reply_as_corrected_write_never_becomes_answer(trial, read_first):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    corrected = call("create_invoice", customer="Wayne", items=[{"item": "Website audit", "quantity": 1}])
    listing = call("list_customers")
    calls = [listing, corrected] if read_first else [corrected, listing]
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", calls)]))
    a.turn("mixed_correction", "Invoice Acme Corporation for one website audit.")
    events = a.turn("mixed_correction", "I meant Wayne")
    assert events[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(events[-1]), events


def test_new_correction_after_question_set_aside_the_card(trial):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")]),
                                      Reply("", [call("list_customers")])]))
    a.turn("question_then_correction", "Invoice Acme Corporation for one website audit.")
    assert a.turn("question_then_correction", "no, Wayne, not Acme")[-1]["type"] == "ask"
    events = a.turn("question_then_correction", "I meant Hooli")
    assert events[-1]["type"] in ("ask", "card") and "Hooli" in str(events[-1]), events


def test_named_record_read_preserves_current_behavior(trial):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    a.turn("named_read", "Invoice Acme Corporation for one website audit.")
    events = a.turn("named_read", "show me Wayne's details")
    assert events[-1]["type"] == "answer", events


def test_what_correction_after_card_set_aside(trial):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("Okay."), Reply("", [call("list_customers")])]))
    a.turn("aside_what", "Invoice Acme Corporation for one website audit.")
    assert a.turn("aside_what", "cancel that")[-1]["type"] == "notice"
    events = a.turn("aside_what", "what I meant was Wayne")
    assert events[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(events[-1]), events
    assert sum(e["type"] == "ask" for e in events) <= 1


def test_correction_to_name_matching_a_read_verb(trial):
    from erpclaw_core.common import company_ctx
    from erpclaw_core.masters import create_party

    with trial.begin() as conn:
        create_party(company_ctx(conn), "customer", "Sell", "sell@example.invalid", "York")
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    a.turn("verb_name", "Invoice Acme Corporation for one website audit.")
    events = a.turn("verb_name", "what I meant was Sell")
    assert events[-1]["type"] in ("ask", "card") and "Sell" in str(events[-1]), events
    assert sum(e["type"] == "ask" for e in events) <= 1


@pytest.mark.parametrize("message", ["show me Acme Corporation's details", "what's Acme Corporation's address?",
                                     "what is the address of Acme Corporation?", "show me Acme Corporation"])
@pytest.mark.parametrize("as_list", [True, False])
def test_named_read_keeps_pending_card_and_books(trial, message, as_list):
    from sqlalchemy import func, select
    from erpclaw_sales import tables as m
    from erpclaw_core import approvals

    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    reply = Reply("", [call("list_customers")]) if as_list else Reply("Here are the details.")
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), reply]))
    a.turn("read_preserves", "Invoice Acme Corporation for one website audit.")
    before = approvals.pending_card(trial, "read_preserves")
    with trial.connect() as conn:
        count = conn.execute(select(func.count()).select_from(m.invoice)).scalar_one()
    events = a.turn("read_preserves", message)
    after = approvals.pending_card(trial, "read_preserves")
    assert events[-1]["type"] == "answer", events
    assert after is not None and (after["id"], after["args_json"]) == (before["id"], before["args_json"])
    assert not any(e.get("code") == "card_set_aside" or e["type"] == "card" for e in events), events
    with trial.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.invoice)).scalar_one() == count


@pytest.mark.parametrize("message", ["I meant Wayne, show me his details", "no, show me Wayne instead"])
@pytest.mark.parametrize("as_list", [True, False])
def test_correction_cue_precedes_read(trial, message, as_list):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    reply = Reply("", [call("list_customers")]) if as_list else Reply("Here are the details.")
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), reply]))
    a.turn("cue_read", "Invoice Acme Corporation for one website audit.")
    events = a.turn("cue_read", message)
    assert events[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(events[-1]), events
    assert sum(e["type"] == "ask" for e in events) <= 1


def test_named_read_ignores_model_write(trial):
    from erpclaw_core import approvals
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    other = call("create_invoice", customer="Wayne", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [other])]))
    a.turn("read_write", "Invoice Acme Corporation for one website audit.")
    before = approvals.pending_card(trial, "read_write")
    events = a.turn("read_write", "show me Acme Corporation's details")
    after = approvals.pending_card(trial, "read_write")
    assert events[-1]["type"] == "answer", events
    assert after is not None and (after["id"], after["args_json"]) == (before["id"], before["args_json"])
    assert not any(e["type"] == "card" or e.get("code") == "card_set_aside" for e in events)


def test_cue_with_read_after_card_set_aside(trial):
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("Okay."), Reply("", [call("list_customers")])]))
    a.turn("aside_read_cue", "Invoice Acme Corporation for one website audit.")
    assert a.turn("aside_read_cue", "cancel that")[-1]["type"] == "notice"
    events = a.turn("aside_read_cue", "I meant Wayne, show me his details")
    assert events[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(events[-1]), events


def test_unrelated_named_read_sets_card_aside(trial):
    from erpclaw_core import approvals
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    first = a.turn("other_read", "Invoice Acme Corporation for one website audit.")[-1]
    events = a.turn("other_read", "show me Wayne's details")
    assert events[0]["code"] == "card_set_aside" and "Ask again to get it back." in events[0]["text"]
    assert events[-1]["type"] == "answer"
    assert approvals.pending_card(trial, "other_read") is None
    assert approvals.approve(trial, first["approval_id"], first["digest"]).status == "superseded"


def test_line_item_read_keeps_card(engine):
    from erpclaw_core import approvals
    from sqlalchemy import func, select
    from erpclaw_sales import tables as sales
    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])
    a = Assistant(engine, ScriptedModel([Reply("", [invoice]), Reply("Here are the details.")]))
    a.turn("item_read", "Invoice Acme Corporation for 5 copper wire")
    before = approvals.pending_card(engine, "item_read")
    with engine.connect() as conn:
        count = conn.execute(select(func.count()).select_from(sales.invoice)).scalar_one()
    events = a.turn("item_read", "what's copper wire's price?")
    after = approvals.pending_card(engine, "item_read")
    assert after is not None and (after["id"], after["args_json"]) == (before["id"], before["args_json"])
    assert events[-1]["type"] == "answer" and not any(e.get("code") == "card_set_aside" for e in events)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(sales.invoice)).scalar_one() == count


def test_short_party_name_read_keeps_matching_card(trial):
    from erpclaw_core import approvals
    invoice = call("create_invoice", customer="Wayne Enterprises", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    a.turn("short_party", "Invoice Wayne Enterprises for one website audit.")
    before = approvals.pending_card(trial, "short_party")
    events = a.turn("short_party", "show me Wayne's details")
    after = approvals.pending_card(trial, "short_party")
    assert after is not None and (after["id"], after["args_json"]) == (before["id"], before["args_json"])
    assert events[-1]["type"] == "answer" and not any(e.get("code") == "card_set_aside" for e in events)

@pytest.mark.parametrize("code,kind", [("customer_missing", "ask"), ("read_denied", "refused")])
@pytest.mark.parametrize("related", [True, False])
def test_read_question_or_refusal_preserves_only_related_card(trial, monkeypatch, code, kind, related):
    from dataclasses import replace
    from sqlalchemy import func, select
    from erpclaw_core import approvals
    from erpclaw_core.common import Refusal
    from erpclaw_sales import tables as sales
    from erpclaw_assistant.graph import REGISTRY

    invoice = call("create_invoice", customer="Acme Corporation", items=[{"item": "Website audit", "quantity": 1}])
    a = Assistant(trial, ScriptedModel([Reply("", [invoice]), Reply("", [call("list_customers")])]))
    a.turn("read_outcome", "Invoice Acme Corporation for one website audit.")
    before = approvals.pending_card(trial, "read_outcome")
    with trial.connect() as conn:
        count = conn.execute(select(func.count()).select_from(sales.invoice)).scalar_one()

    def read_outcome(ctx, inp):
        raise Refusal("Read tool outcome", code)

    contract = REGISTRY.contracts["list_customers"]
    monkeypatch.setitem(REGISTRY.contracts, "list_customers", replace(contract, run=read_outcome))
    name = "Acme Corporation" if related else "Wayne"
    events = a.turn("read_outcome", f"show me {name}'s details")
    after = approvals.pending_card(trial, "read_outcome")
    assert events[-1]["type"] == kind and events[-1]["text"] == "Read tool outcome", events
    if related:
        assert after is not None and (after["id"], after["args_json"]) == (before["id"], before["args_json"])
        assert not any(e.get("code") == "card_set_aside" or e["type"] == "card" for e in events)
    else:
        assert after is None and any(e.get("code") == "card_set_aside" for e in events)
    with trial.connect() as conn:
        assert conn.execute(select(func.count()).select_from(sales.invoice)).scalar_one() == count
