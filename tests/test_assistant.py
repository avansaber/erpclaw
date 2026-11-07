"""API end to end with a scripted model, the reply parser, the number check."""
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call, clean_reply
from erpclaw_sales import tables as t


def client(engine, replies):
    model = ScriptedModel(replies)
    c = TestClient(create_app(engine, model, embedded_owner=True))  # in-process: the owner
    return c, c.post("/v1/conversations").json()["id"], model


def test_22_message_card_approve_result(engine):
    c, cid, _ = client(engine, [Reply("", [call("create_invoice", customer="Acme Corporation",
                                                 items=[{"item": "copper wire", "quantity": 5}])])])
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Acme Corporation for 5 copper wire"}).json()["events"]
    assert [e["type"] for e in ev] == ["card"]
    card = ev[0]
    assert ["Total", "140.00 USD"] in card["card"]["lines"]
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()
    ev2 = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert ev2[0]["type"] == "result" and ev2[0]["status"] == "committed" and "INV-2026-" in ev2[0]["text"]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.invoice)).scalar_one() == before + 1
    rb = c.get(f"/v1/operations/{ev2[0]['operation_key']}").json()
    assert rb["status"] == "committed"


def test_22b_read_is_rendered_by_code_and_ambiguity_asks(engine):
    c, cid, model = client(engine, [Reply("", [call("get_metric", metric="receivables")]),
                                    Reply("", [call("create_invoice", customer="ABC",
                                                    items=[{"item": "circuit board", "quantity": 3}])])])
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "who owes us money?"}).json()["events"]
    assert ev[0]["type"] == "answer" and "4,870.00 USD" in ev[0]["text"]
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice ABC for 3 circuit boards"}).json()["events"]
    assert ev[0]["type"] == "ask" and "can't find a customer" in ev[0]["text"]
    assert len(model.seen) == 2
    history = [m.get("content") or "" for m in model.seen[1] if m["role"] == "assistant"]
    assert any("4,870.00" in h for h in history)


def test_22c_bad_arguments_get_one_precise_retry(engine):
    """Arguments the engine cannot read (lines that are not a list of lines) go back to the model once."""
    lines = [{"item": "copper wire", "quantity": 5}]
    c, cid, model = client(engine, [Reply("", [call("create_invoice", customer="Acme Corporation",
                                                    items="5 copper wire")]),
                                    Reply("", [call("create_invoice", customer="Acme Corporation", items=lines)])])
    ev = c.post(f"/v1/conversations/{cid}/messages",
                json={"text": "invoice Acme Corporation for 5 copper wire"}).json()["events"]
    assert [e["type"] for e in ev] == ["card"]
    assert "not valid" in model.seen[1][-1]["content"]


def test_22d_lines_the_model_left_out_come_from_the_owner_s_words_without_a_retry(engine):
    c, cid, model = client(engine, [Reply("", [call("create_invoice", customer="Acme Corporation", items=[])])])
    ev = c.post(f"/v1/conversations/{cid}/messages",
                json={"text": "invoice Acme Corporation for 5 copper wire"}).json()["events"]
    assert [e["type"] for e in ev] == ["card"] and len(model.seen) == 1
    assert any(k == "Item" and v.startswith("5 x Copper Wire") for k, v in ev[0]["card"]["lines"])


def test_23_parser():
    r = clean_reply("<think>let me see</think>Sales were good.", [])
    assert r.text == "Sales were good." and r.reasoning_chars > 0
    r = clean_reply("<think>The answer is: you have 3 customers.", [])  # unfinished thought: never the answer
    assert r.text == "" and not r.reasoning_as_answer
    r = clean_reply('<tool_call>\n{"name": "get_metric", "arguments": {"metric": "sales"}}\n</tool_call>', [])
    assert r.calls == [] and r.text_calls == 1 and r.text == ""      # text calls are never executed
    r = clean_reply("", [], reasoning='<tool_call>{"name": "create_invoice", "arguments": {}}</tool_call>')
    assert r.calls == [] and r.text_calls == 1
    r = clean_reply("", [("c1", "create_invoice", '{"customer": "Acme Corporation", "items": [{"item": "x", "quantity": 2.5')])
    assert r.calls[0].args is None and "complete JSON" in r.calls[0].error
    r = clean_reply("", [("c1", "record_payment", '{"customer": "Hooli", "amount": 10.10}')])
    assert r.calls[0].args["amount"] == Decimal("10.10")


def test_23b_text_call_gets_one_reprompt_and_writes_nothing(engine):
    txt = Reply("")
    txt.text_calls = 1
    c, cid, model = client(engine, [txt, txt, txt, txt, txt])
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Acme Corporation for 5 copper wire"}).json()["events"]
    assert all(e["type"] != "card" for e in ev)
    assert "not run" in model.seen[1][-1]["content"]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.invoice)).scalar_one() == before


def test_24_number_check_blocks_invented_figures(engine):
    c, cid, _ = client(engine, [Reply("Your MRR is 12,500.00 this month.")])
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "how are we doing this month?"}).json()["events"]
    assert ev[0]["type"] == "answer" and "12,500" not in ev[0]["text"] and "come from your books" in ev[0]["text"]


def test_24b_a_planned_figure_the_owner_named_gets_the_planned_reply(engine):
    """"MRR" names subscriptions by a synonym: a tool-less reply gets the catalogue's planned reply, never the text."""
    c, cid, _ = client(engine, [Reply("Your MRR is 12,500.00 this month.")])
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "what's our MRR?"}).json()["events"]
    assert (ev[0]["type"], ev[0].get("capability"), ev[0].get("checked")) == (
        "answer", "planned.subscriptions", "engine"), ev



@pytest.mark.parametrize("raw, shown", [
    ("\x1b[407m", ""),                                   # a live reply that was one bare colour escape
    ("Total \x1b[31m140.00\x1b[0m due", "Total 140.00 due"),
    ("a\x1b]0;owned\x07b", "ab"),                        # a window-title OSC
    ("paid\rUNPAID", "paidUNPAID"),                      # a carriage return that rewrites the line
    ("x\x00\x08\x7f\x9cy", "xy"),                       # bare C0, DEL and a bare C1 (8-bit CSI is a sequence)
])
def test_terminal_escapes_and_control_characters_never_reach_the_owner(raw, shown):
    reply = clean_reply(raw, [])
    assert reply.text == shown and reply.control_chars == len(raw) - len(shown), (reply.text, reply.control_chars)


def test_newlines_tabs_numbers_and_ordinary_text_are_unchanged():
    text = "Invoice INV-2026-00009\tAcme Corporation\n3 x Consulting at 150.00 = 450.00 (cafe, naive, 100%)"
    reply = clean_reply(text, [])
    assert reply.text == text and reply.control_chars == 0



@pytest.mark.parametrize("raw, shown", [
    ("\x9b31mPaid\x9b0m 140.00", "Paid 140.00"),            # an 8-bit CSI is removed whole
    ("a\x9d0;title\x9cb 450.00", "ab 450.00"),              # an 8-bit OSC ends at the 8-bit string terminator
    ("a\x1b]0;title\x9cb", "ab"),                           # a 7-bit OSC ended by the 8-bit terminator
])
def test_eight_bit_sequences_are_removed_whole_and_neighbouring_text_kept(raw, shown):
    assert clean_reply(raw, []).text == shown


def test_a_tool_calls_arguments_carry_no_control_characters():
    """Native call arguments lose terminal control sequences before reaching the API and the window."""
    raw = '{"customer": "\\u001b[31mZork Ltd\\u001b[0m", "items": [{"item": "x\\u009b1m", "quantity": 1}]}'
    reply = clean_reply("", [("c1", "create_invoice", raw)])
    assert reply.calls[0].args == {"customer": "Zork Ltd", "items": [{"item": "x", "quantity": 1}]}
    assert reply.control_chars == 12


def test_an_escaped_unknown_customer_never_reaches_the_events(engine):
    raw = '{"customer": "\\u001b[31mZork Ltd\\u001b[0m", "items": [{"item": "copper wire", "quantity": 1}]}'
    c, cid, _ = client(engine, [clean_reply("", [("c1", "create_invoice", raw)])])
    events = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice them"}).json()["events"]
    text = json.dumps(events)
    # a name the owner did not write is never quoted back, escaped or not: the engine asks whom
    assert "\\u001b" not in text and "\\u009b" not in text and "Zork" not in text, events
    assert [e["code"] for e in events] == ["customer_missing"]
