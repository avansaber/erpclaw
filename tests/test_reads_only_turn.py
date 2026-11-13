"""A read cannot silently finish a write or planned request."""
import pytest

from bookdiff import snapshot
from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import catalogue as cat
from erpclaw_core.plugins import disable_plugin


def run(engine, said, replies):
    model = ScriptedModel(replies)
    assistant = Assistant(engine, model)
    return assistant, model, assistant.turn("reads-only", said)


def reads():
    return [Reply("", [call("list_customers")]), Reply("", [call("list_items")])]


def invoice(name, item, quantity):
    return Reply("", [call("create_invoice", customer=name, items=[{"item": item, "quantity": quantity}])])


def test_planned_request_after_read_uses_catalogue_reply(engine, as_released):
    said = "how many invoices did Wayne get this year?"
    before = snapshot(engine)
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
        top = max(score for _, score in named)
        tied = [entry for entry, score in named if score == top]
        decision = cat.decide(tied, tied[0].id)
    _, _, events = run(engine, said, [Reply("", [call("list_customers")]), Reply("I cannot tell.")])
    assert len(events) == 1 and events[0].get("outcome") == "c" and events[0]["text"] == decision.text, events
    assert "not in ERPClaw yet" in events[0]["text"]
    assert any(row[2:5] == ("c", decision.entry.id, said) for row in store.demand())
    assert snapshot(engine) == before


@pytest.mark.parametrize("name,item,quantity,extra,canonical", [
    ("Acme", "copper wire", 5, "", "Acme Corporation"),
    ("Wayne", "circuit boards", 2, " and email it", "Wayne Enterprises"),
], ids=["W1", "W5"])
def test_write_after_reads_reaches_resolution(engine, as_released, name, item, quantity, extra, canonical):
    said = f"invoice {name} for {quantity} {item}{extra}"
    before = snapshot(engine)
    assistant, model, events = run(engine, said, reads() + [Reply("I can help with that."),
                                                             invoice(name, item, quantity),
                                                             invoice(canonical, item, quantity)])
    assert len(events) == 1 and events[0].get("code") == "customer_ambiguous", events
    assert events[0]["options"][0] == canonical
    assert snapshot(engine) == before
    assert model.tools_seen[-1] == ["create_invoice"]
    tool_text = " ".join(message["content"] for message in model.seen[-1] if message["role"] == "tool")
    assert canonical in tool_text and "Copper Wire" in tool_text
    if extra:
        choice = next(c for c in events[0]["choices"] if c["label"] == canonical)
        result = assistant.turn("reads-only", canonical, choice=choice["code"])
        assert any(e["type"] == "card" for e in result), result
        assert any(e.get("part") == "email" and e.get("capability") == "planned.messaging" for e in result), result


@pytest.mark.parametrize("said,tool,args,expected", [
    ("list my customers", "list_customers", {}, "Total customers:"),
    ("what does Acme owe", "list_invoices", {"customer": "Acme Corporation"}, "Acme Corporation"),
])
def test_read_request_keeps_direct_answer(engine, said, tool, args, expected):
    _, _, events = run(engine, said, [Reply("", [call(tool, **args)]), Reply("unused")])
    assert len(events) == 1 and events[0]["type"] == "answer" and expected in events[0]["text"], events


def test_read_error_then_text_keeps_checked_text(engine, as_released):
    text = "Here are your customers."
    _, _, events = run(engine, "list my customers", [Reply("", [call("list_customers", invalid_field=1)]), Reply(text)])
    assert len(events) == 1 and events[0]["text"] == text and events[0]["checked"] == "passed", events


def test_write_call_still_resolves(engine, as_released):
    _, _, events = run(engine, "invoice Acme for 5 copper wire", [invoice("Acme", "copper wire", 5)])
    assert len(events) == 1 and events[0].get("code") == "customer_ambiguous", events


def test_toolless_installed_request_still_keeps_text(engine, as_released):
    text = "What quantity should I invoice?"
    _, _, events = run(engine, "invoice Acme for copper wire", [Reply(text)])
    assert len(events) == 1 and events[0]["text"] == text and events[0]["checked"] == "passed", events


def test_read_answer_still_checks_figures(engine, as_released):
    _, _, events = run(engine, "list my customers", [Reply("", [call("list_customers", invalid_field=1)]),
                                                      Reply("There are 987654 customers.")])
    assert len(events) == 1 and events[0]["checked"] == "replaced" and "987654" not in events[0]["text"], events


def test_text_on_selected_path_stops_at_one_question(engine, as_released):
    before = snapshot(engine)
    _, model, events = run(engine, "invoice Acme for 5 copper wire", reads() + [Reply("Wait.")] * 3)
    assert len(model.seen) == 4
    assert len(events) == 1 and events[0]["type"] == "ask", events
    assert snapshot(engine) == before


@pytest.mark.parametrize("said", ["how much money is owed by customers?",
                                  "show me all customers, including those with money owed"])
def test_customer_list_and_planned_receivables(engine, as_released, said):
    before = snapshot(engine)
    with engine.connect() as conn:
        entry = next(e for e, _ in cat.named_by_synonym(conn, said) if e.id == "metric.receivables")
        decision = cat.decide([entry], entry.id)
    _, _, events = run(engine, said, [Reply("", [call("list_customers")]), Reply("Here are your customers.")])
    assert len(events) == 2, events
    assert events[0].get("outcome") == "c" and events[0]["text"] == decision.text, events
    assert events[1]["type"] == "answer" and "Here are your customers:" in events[1]["text"], events
    assert "Total customers:" in events[1]["text"] and not events[1]["amounts"], events
    assert snapshot(engine) == before


def test_combined_reply_omits_money_from_read_result(engine, as_released):
    before = snapshot(engine)
    _, _, events = run(engine, "list my items and money owed", [Reply("", [call("list_items")])])
    assert len(events) == 2 and events[0].get("outcome") == "c", events
    assert "Here are your items:" in events[1]["text"] and "Copper Wire" in events[1]["text"], events
    assert "[amount omitted]" in events[1]["text"] and events[1]["amounts"] == [], events
    assert snapshot(engine) == before


def test_combined_read_does_not_attach_money_steps(engine, as_released, monkeypatch):
    monkeypatch.setattr("erpclaw_assistant.graph._read_step", lambda *args: ("lookup", "USD 123.00"))
    _, _, events = run(engine, "list my customers and money owed", [Reply("", [call("list_customers")])])
    assert len(events) == 2 and events[0].get("outcome") == "c", events
    assert "Here are your customers:" in events[1]["text"] and events[1]["amounts"] == [], events
    assert all(not e.get("steps") for e in events), events


def test_combined_reply_for_available_capability(engine, as_released):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    said = "list my customers and create invoice"
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
        entry = max((e for e, _ in named if e.state == "available"),
                    key=lambda e: next(score for candidate, score in named if candidate.id == e.id))
        decision = cat.decide([entry], entry.id, cat.switched_on(conn))
    _, _, events = run(engine, said, [Reply("", [call("list_customers")])])
    assert len(events) == 2 and events[0]["text"] == decision.text, events
    assert "Here are your customers:" in events[1]["text"], events


def test_follow_up_reads_continue_open_invoice_request(engine, as_released):
    from decimal import Decimal
    from erpclaw_core.common import company_ctx
    from erpclaw_core.masters import create_item

    with engine.begin() as conn:
        create_item(company_ctx(conn), "Website audit", Decimal("900.00"))
    before = snapshot(engine)
    model = ScriptedModel(reads() + [Reply("Which customer?")] * 2 +
                          [Reply("", [call("list_customers"), call("list_items")]), Reply("Here are the results."),
                           invoice("Acme Corporation", "Website audit", 1)])
    assistant = Assistant(engine, model)
    first = assistant.turn("follow-up", "Invoice either Acme Corporation or Hooli for one website audit.")
    assert len(first) == 1 and first[0]["type"] == "ask", first
    events = assistant.turn("follow-up", "Whichever is fine, just do it.")
    assert len(events) == 1 and events[0]["type"] == "ask", events
    assert events[0]["code"] in ("customer_missing", "customer_ambiguous"), events
    assert set(events[0]["options"]) >= {"Acme Corporation", "Hooli"}, events
    assert model.tools_seen[-1] == ["create_invoice"]
    assert snapshot(engine) == before


def test_new_read_request_after_open_invoice_keeps_list_answer(engine, as_released):
    before = snapshot(engine)
    model = ScriptedModel(reads() + [Reply("Which customer?")] * 2 +
                          [Reply("", [call("list_customers")])])
    assistant = Assistant(engine, model)
    first = assistant.turn("new-read", "Invoice either Acme Corporation or Hooli for one website audit.")
    assert len(first) == 1 and first[0]["type"] == "ask", first
    events = assistant.turn("new-read", "list my customers")
    assert len(events) == 1 and events[0]["type"] == "answer" and "Total customers:" in events[0]["text"], events
    assert snapshot(engine) == before


@pytest.mark.parametrize("checkpointed", [False, True], ids=["before_events", "after_resolution"])
def test_retry_follow_up_after_reads_matches_first_attempt(engine, as_released, monkeypatch, checkpointed):
    from datetime import datetime, timedelta, timezone

    now = [datetime(2026, 11, 8, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "_now", lambda: now[0])
    initial = "Invoice either Acme Corporation or Hooli for one website audit."
    follow_up = "Whichever is fine, just do it."

    def start(cid):
        assistant = Assistant(engine, ScriptedModel(reads() + [Reply("Which customer?")] * 2))
        assert assistant.turn(cid, initial)[0]["type"] == "ask"

    def follow_model():
        return ScriptedModel([Reply("", [call("list_customers"), call("list_items")]),
                              Reply("Here are the results."),
                              invoice("Acme Corporation", "Website audit", 1)])

    start("reference")
    expected = Assistant(engine, follow_model()).turn("reference", follow_up, client_message_id="follow")
    start("interrupted")
    interrupted = Assistant(engine, follow_model())
    with monkeypatch.context() as patch:
        def stop(*args, **kwargs):
            raise RuntimeError("interrupted")

        if checkpointed:
            patch.setattr(store, "finalise", stop)
        else:
            patch.setattr(interrupted.graph, "invoke", stop)
        with pytest.raises(RuntimeError, match="interrupted"):
            interrupted.turn("interrupted", follow_up, client_message_id="follow")
    now[0] += store.LEASE + timedelta(seconds=1)
    actual = Assistant(engine, follow_model()).turn("interrupted", follow_up, client_message_id="follow")
    assert actual == expected
