"""An incomplete model reply must leave an existing approval usable."""
import pytest
from sqlalchemy import func, select

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_sales import tables
from test_turn_correction_gate import books, trial  # shared isolated service-company fixture


def invoice_count(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one()


@pytest.mark.parametrize("reply,expected", [
    (Reply("", finish="stop"), {"type": "answer", "text": "I didn't get an answer for that. Could you say it another way?",
                                "checked": "engine", "currency": "USD"}),
    (Reply("", finish="length"), {"type": "refused", "code": "answer_interrupted",
                                  "text": "The answer was cut short before it finished.", "currency": "USD"}),
], ids=["empty", "length"])
def test_incomplete_reply_without_pending_card_preserves_original_behavior(trial, reply, expected):
    assistant = Assistant(trial, ScriptedModel([reply]))
    assert assistant.turn("no-card", "hello") == [expected]


@pytest.mark.parametrize("reply", [Reply("", finish="length"), Reply("")], ids=["length", "empty"])
def test_incomplete_reply_keeps_h014_card(trial, reply):
    invoice = call("create_invoice", customer="Initech", items=[{"item": "Support retainer", "quantity": 1}], submit=True)
    assistant = Assistant(trial, ScriptedModel([Reply("", [invoice]), reply]))
    before = invoice_count(trial)
    [card] = assistant.turn("h014", "Invoice Initech for one support retainer.")
    assert card["type"] == "card" and invoice_count(trial) == before
    events = assistant.turn("h014", "yes approve it")
    assert events == [{"type": "refused", "code": "answer_interrupted",
                       "text": "The answer was cut short before it finished. Your approval card is still pending.",
                       "currency": "USD"}]
    assert invoice_count(trial) == before
    assert store.open_request("h014")["pending_card"] == card["approval_id"]
    assert approvals.pending_card(trial, "h014")["id"] == card["approval_id"]
    first = approvals.approve(trial, card["approval_id"], card["digest"])
    assert first.status == "committed" and invoice_count(trial) == before + 1
    again = approvals.approve(trial, card["approval_id"], card["digest"])
    assert again.status == "committed" and again.operation_key == first.operation_key
    assert invoice_count(trial) == before + 1
