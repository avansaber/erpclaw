"""An unknown invoice name can be added before continuing the invoice."""
import pytest

from bookdiff import assert_changes, changes, snapshot
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from ownership_helpers import owner_check


def invoice(*items):
    return call("create_invoice", customer="ABC", items=list(items))


def offer(assistant, engine, message="invoice ABC for 2 copper wire"):
    before = snapshot(engine)
    events = assistant.turn("conversation", message)
    assert [e["type"] for e in events] == ["ask"]
    assert {"Add it", "No thanks"} <= {c["label"] for c in events[0]["choices"]}
    assert any(c["label"] == "Cancel" and c["control"] for c in events[0]["choices"])
    assert_changes(before, snapshot(engine), {})
    return events[0]


def approved_customer(assistant, engine, message="invoice ABC for 2 copper wire"):
    offer(assistant, engine, message)
    [event] = assistant.turn("conversation", "Add it")
    assert event["type"] == "card"
    assert approvals.approve(engine, event["approval_id"], event["digest"],
                             ownership_check=owner_check(engine)).status == "committed"


def test_item_details_are_separate_from_held_invoice(engine):
    inv = invoice({"item": "widgets", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])] * 10))
    before = snapshot(engine)
    [event] = assistant.turn("conversation", "invoice ABC for 2 widgets at 30")
    assert event["type"] == "ask"
    assert "ABC" in event["text"] and "widgets" in event["text"]
    assert_changes(before, snapshot(engine), {})
    [event] = assistant.turn("conversation", "Add them")
    assert event["type"] == "card"
    assert approvals.approve(engine, event["approval_id"], event["digest"],
                             ownership_check=owner_check(engine)).status == "committed"
    [event] = assistant.turn("conversation", "continue")
    assert event["type"] == "ask" and event["code"] == "item_kind_missing"
    [event] = assistant.turn("conversation", "A service I sell")
    assert event["type"] == "ask" and event["code"] == "item_unit_missing"
    [event] = assistant.turn("conversation", "hour")
    assert event["type"] == "ask" and event["code"] == "item_price_missing"
    [event] = assistant.turn("conversation", "30")
    assert event["type"] == "card", event
    item_card = str(event["card"])
    assert "widgets" in item_card and "30" in item_card
    assert approvals.approve(engine, event["approval_id"], event["digest"],
                             ownership_check=owner_check(engine)).status == "committed"
    events = assistant.turn("conversation", "continue")
    cards = [e for e in events if e["type"] == "card"]
    assert len(cards) == 1, events
    lines = cards[0]["card"]["lines"]
    assert ["Customer", "ABC"] in lines
    assert "widgets" in str(lines).lower() and "2" in str(lines) and "30" in str(lines)


def test_post_approval_guidance_is_visible_in_next_turn(engine):
    inv = invoice({"item": "copper wire", "quantity": 2})
    model = ScriptedModel([Reply("", [inv])] * 8)
    assistant = Assistant(engine, model)
    approved_customer(assistant, engine)
    events = assistant.turn("conversation", "continue")
    assert any("continue" in e.get("text", "").lower() or "ready" in e.get("text", "").lower()
               for e in events)


def test_offer_explains_how_to_continue_before_answer(engine):
    inv = invoice({"item": "copper wire", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])] * 8))
    event = offer(assistant, engine)
    assert "continue" in event["text"].lower()


def test_each_unknown_item_gets_its_own_details(engine):
    inv = invoice({"item": "widget", "quantity": 2}, {"item": "sprocket", "quantity": 3})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])] * 12))
    [event] = assistant.turn("conversation", "invoice ABC for 2 widgets and 3 sprockets")
    assert event["type"] == "ask"
    for unit, price, name in [("hour", "30", "widgets"), ("meter", "45", "sprockets")]:
        [event] = assistant.turn("conversation", "Add them" if name == "widgets" else "continue")
        if name == "widgets":
            assert event["type"] == "card"
            assert approvals.approve(engine, event["approval_id"], event["digest"],
                                     ownership_check=owner_check(engine)).status == "committed"
            [event] = assistant.turn("conversation", "continue")
        assert event["type"] == "ask" and event["code"] == "item_kind_missing", event
        [event] = assistant.turn("conversation", "A service I sell")
        assert event["type"] == "ask" and event["code"] == "item_unit_missing", event
        [event] = assistant.turn("conversation", unit)
        assert event["type"] == "ask" and event["code"] == "item_price_missing", event
        [event] = assistant.turn("conversation", price)
        assert event["type"] == "card", event
        lines = str(event["card"]["lines"]).lower()
        assert name in lines and unit in lines and price in lines
        assert approvals.approve(engine, event["approval_id"], event["digest"],
                                 ownership_check=owner_check(engine)).status == "committed"


@pytest.mark.parametrize("changed", [
    call("record_payment", customer="ABC", amount=2),
    invoice({"item": "copper wire", "quantity": 2}),
], ids=["different-operation", "dropped-line"])
def test_resume_rejects_changed_model_call(engine, changed):
    original = invoice({"item": "copper wire", "quantity": 2}, {"item": "circuit board", "quantity": 3})
    model = ScriptedModel([Reply("", [original]), Reply("", [changed])] * 8)
    assistant = Assistant(engine, model)
    approved_customer(assistant, engine, "invoice ABC for 2 copper wire and 3 circuit board")
    before = snapshot(engine)
    events = assistant.turn("conversation", "continue")
    assert not any(e["type"] == "card" for e in events)
    assert_changes(before, snapshot(engine), {})


def test_different_write_does_not_resume_held_invoice(engine):
    inv = invoice({"item": "copper wire", "quantity": 2})
    model = ScriptedModel([Reply("", [inv]), Reply("", [call("record_payment", customer="Hooli", amount=500)])] * 8)
    assistant = Assistant(engine, model)
    approved_customer(assistant, engine)
    events = assistant.turn("conversation", "Hooli paid 500")
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert len(cards) == 1
    assert "ABC" not in str(cards[0]) and "copper wire" not in str(cards[0]).lower()


@pytest.mark.parametrize("answer", ["yes", "continue", "go ahead", "yes please", "y", "go on"])
def test_affirmative_continuation_resumes(engine, answer):
    inv = invoice({"item": "copper wire", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])] * 8))
    approved_customer(assistant, engine)
    events = assistant.turn("conversation", answer)
    assert any(e["type"] in ("card", "ask") for e in events), events


@pytest.mark.parametrize("answer", ["no", "no thanks", "cancel", "stop", "never mind"])
def test_negative_continuation_does_not_resume(engine, answer):
    inv = invoice({"item": "copper wire", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])] * 8))
    approved_customer(assistant, engine)
    before = snapshot(engine)
    events = assistant.turn("conversation", answer)
    assert any(e["type"] == "answer" for e in events), events
    assert not any(e["type"] == "card" for e in events), events
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize("answer", ["no", "no thanks", "cancel"])
def test_decline_offer_writes_nothing(engine, answer):
    inv = invoice({"item": "copper wire", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])] * 8))
    offer(assistant, engine)
    before = snapshot(engine)
    events = assistant.turn("conversation", answer)
    assert any(e["type"] == "answer" for e in events), events
    assert not any(e["type"] == "card" for e in events), events
    assert_changes(before, snapshot(engine), {})


def test_offer_lists_unknown_lines_in_owner_order_and_keeps_plural(engine):
    inv = invoice({"item": "widget", "quantity": 2}, {"item": "sprocket", "quantity": 3})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])]))
    before = snapshot(engine)
    [event] = assistant.turn("conversation", "invoice ABC for 2 widgets and 3 sprockets")
    assert event["type"] == "ask"
    text = event["text"]
    assert text.index("ABC") < text.index("widgets") < text.index("sprockets")
    assert_changes(before, snapshot(engine), {})


def test_known_customer_follows_normal_invoice_path(engine):
    inv = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])]))
    before = snapshot(engine)
    [event] = assistant.turn("conversation", "invoice Acme Corporation for 2 copper wire")
    assert event["type"] == "card", event
    assert_changes(before, snapshot(engine), {"approval": {"added": [{"status": "pending"}]}})
    assert approvals.approve(engine, event["approval_id"], event["digest"],
                             ownership_check=owner_check(engine)).status == "committed"


def test_held_invoice_does_not_cross_conversations(engine):
    inv = invoice({"item": "copper wire", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv]), Reply("What would you like to do?")]))
    approved_customer(assistant, engine)
    before = snapshot(engine)
    events = assistant.turn("other-conversation", "continue")
    assert not any(e["type"] == "card" for e in events), events
    assert_changes(before, snapshot(engine), {})


def test_cancel_pending_add_does_not_write_customer(engine):
    inv = invoice({"item": "copper wire", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])] * 8))
    offer(assistant, engine)
    [event] = assistant.turn("conversation", "Add it")
    assert event["type"] == "card"
    before = snapshot(engine)
    events = assistant.turn("conversation", "cancel")
    assert not any(e["type"] == "card" for e in events), events
    # Only the already-proposed approval may change status.
    diff = changes(before, snapshot(engine))
    assert set(diff) <= {"approval"}, diff
    assert all(new["status"] == "declined" for _, new in diff.get("approval", {}).get("changed", []))


def test_near_customer_is_a_choice_not_an_offer(engine):
    inv = call("create_invoice", customer="Acme Corp", items=[{"item": "copper wire", "quantity": 2}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])]))
    before = snapshot(engine)
    [event] = assistant.turn("conversation", "invoice Acme Corp for 2 copper wire")
    assert event["type"] == "ask" and "Acme Corporation" in event["options"], event
    assert "Add it" not in [c["label"] for c in event["choices"]]
    assert_changes(before, snapshot(engine), {})


def test_near_item_is_a_choice_not_an_offer(engine):
    inv = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wir", "quantity": 2}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])]))
    before = snapshot(engine)
    [event] = assistant.turn("conversation", "invoice Acme Corporation for 2 copper wir")
    assert event["type"] == "ask" and event["options"], event
    assert "Add it" not in [c["label"] for c in event["choices"]]
    assert_changes(before, snapshot(engine), {})


def test_model_only_name_never_offers(engine):
    inv = invoice({"item": "copper wire", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])]))
    before = snapshot(engine)
    events = assistant.turn("conversation", "invoice them for 2 copper wire")
    assert not any(e["type"] == "card" for e in events), events
    assert all("Add it" not in [c["label"] for c in e.get("choices", [])] for e in events)
    assert_changes(before, snapshot(engine), {})


def test_partial_owner_name_never_offers(engine):
    inv = invoice({"item": "copper wire", "quantity": 2})
    assistant = Assistant(engine, ScriptedModel([Reply("", [inv])]))
    before = snapshot(engine)
    events = assistant.turn("conversation", "invoice ABC Studios for 2 copper wire")
    assert not any(e["type"] == "card" for e in events), events
    assert all(not ("ABC'" in e.get("text", "") and "Add it" in str(e)) for e in events)
    assert_changes(before, snapshot(engine), {})
