"""The tax next step is real, and declining installed entries stays honest."""
from decimal import Decimal

from sqlalchemy import select

from bookdiff import snapshot
from erpclaw_assistant import resolve_text, store
from erpclaw_assistant.graph import Assistant, FIND_TOOL
from erpclaw_assistant.model import Reply, ScriptedModel, call
from ownership_helpers import owner_check
from erpclaw_core import approvals, catalogue as cat
from erpclaw_sales import tables as sales


TAX_REPLY = ("Sales tax isn't available yet, so I haven't drafted this invoice. "
             "If you send the same invoice without tax words, I will draft it with no tax line and no tax is calculated.")
GENERAL = "I didn't do anything with that. If you want to do one of those, say so with the details."


def test_tax_next_step_refuses_then_drafts_without_tax(engine, as_released):
    tool = call("create_invoice", customer="Hooli", submit=False,
                items=[{"item": "copper wire", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [tool]), Reply("", [tool])]))
    before = snapshot(engine)
    events = assistant.turn("tax-first", "draft an invoice for Hooli for 5 copper wire and add 8% sales tax")
    assert events == [{"type": "answer", "outcome": "c", "capability": "planned.tax_engine", "currency": "USD",
                       "text": TAX_REPLY}]
    assert snapshot(engine) == before
    events = assistant.turn("tax-next", "draft an invoice for Hooli for 5 copper wire")
    assert [e["type"] for e in events] == ["card"]
    assert ["Tax", "No tax calculated: no tax template is set. That is not a statement that no tax is owed."] in events[0]["card"]["lines"]
    result = approvals.approve(engine, events[0]["approval_id"], events[0]["digest"], ownership_check=owner_check(engine))
    assert result.status == "committed"
    with engine.connect() as conn:
        invoice = conn.execute(select(sales.invoice).where(sales.invoice.c.operation_key == result.operation_key)).mappings().one()
        lines = conn.execute(select(sales.invoice_line).where(sales.invoice_line.c.invoice_id == invoice["id"])).mappings().all()
    assert invoice["status"] == "draft"
    assert len(lines) == 1 and lines[0]["quantity"] == Decimal("5")
    assert lines[0]["amount"] == invoice["total"] == Decimal("140.00")


def test_none_over_multiple_installed_names_is_honest_and_does_not_write(engine, as_released):
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
    invoice, supplier = entries["op.create_invoice"], entries["op.add_supplier"]
    assert invoice.state == supplier.state == "installed"
    before, demand = snapshot(engine), store.demand()
    result = cat.decide([invoice, supplier], "none", named=[(invoice, 3), (supplier, 2)])
    assert (result.outcome, result.entry, result.capability, result.text) == ("d", None, cat.NO_MATCH, GENERAL)
    assert snapshot(engine) == before and store.demand() == demand
    tied = [(invoice, 3), (supplier, 3)]
    assert cat.decide([invoice, supplier], "none", named=tied) == cat.decide([invoice, supplier], "unsure", named=tied)


def test_finder_description_is_not_a_menu_instruction():
    assert FIND_TOOL["function"]["description"] == (
        "Search what ERPClaw can do. Pass the owner's own words. Then call choose_capability.")


def test_submit_draft_with_tax_next_step_submits(engine, as_released):
    draft = call("create_invoice", customer="Hooli", submit=False,
                 items=[{"item": "copper wire", "quantity": 5}])
    maker = Assistant(engine, ScriptedModel([Reply("", [draft])]))
    card = maker.turn("make-draft", "draft an invoice for Hooli for 5 copper wire")[0]
    result = approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=owner_check(engine))
    with engine.connect() as conn:
        number = conn.execute(select(sales.invoice.c.number).where(
            sales.invoice.c.operation_key == result.operation_key)).scalar_one()
    submit = call("submit_invoice", invoice=number)
    assistant = Assistant(engine, ScriptedModel([Reply("", [submit]), Reply("", [submit])]))
    reply = assistant.turn("submit-tax", f"submit invoice {number} with tax")
    assert reply[0]["capability"] == "planned.tax_engine"
    assert "submit" in reply[0]["text"].lower()
    assert "will draft" not in reply[0]["text"].lower()
    events = assistant.turn("submit-no-tax", f"submit invoice {number}")
    assert [e["type"] for e in events] == ["card"]
    assert "submit" in events[0]["card"]["title"].lower()


def test_tax_and_discount_has_no_promised_next_step(engine, as_released):
    tool = call("create_invoice", customer="Hooli", submit=False,
                items=[{"item": "copper wire", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [tool]), Reply("", [tool])]))
    reply = assistant.turn("both", "draft an invoice for Hooli for 5 copper wire with tax and a discount")
    assert reply[0]["capability"] == "planned.tax_engine"
    assert "will draft" not in reply[0]["text"].lower()
    following = assistant.turn("discount-remains", "draft an invoice for Hooli for 5 copper wire with a discount")
    assert following[0]["capability"] == "planned.discounts"


def test_other_unsupported_parts_before_tax_have_no_next_step(engine, as_released):
    tool = call("create_invoice", customer="Hooli", submit=False,
                items=[{"item": "copper wire", "quantity": 5}])
    for suffix in ("a discount and add tax", "payment terms and add tax"):
        assistant = Assistant(engine, ScriptedModel([Reply("", [tool])]))
        reply = assistant.turn("other-" + suffix, f"draft an invoice for Hooli for 5 copper wire with {suffix}")
        assert reply[0]["type"] == "answer"
        assert "will draft" not in reply[0]["text"].lower()


def test_create_and_submit_tax_reply_uses_validated_submit_default_and_explicit_true(engine, as_released):
    for extra in ({}, {"submit": True}):
        tool = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}], **extra)
        assistant = Assistant(engine, ScriptedModel([Reply("", [tool])]))
        reply = assistant.turn("submit-" + str(extra), "invoice Hooli for 5 copper wire and add 8% sales tax")
        assert reply[0]["text"] == resolve_text.table()["text"]["tax_submit_next"]
        assert "processed" not in reply[0]["text"].lower()


def test_invalid_invoice_with_tax_has_no_next_step(engine, as_released):
    tool = call("create_invoice", customer="Unknown customer", submit=False,
                items=[{"item": "copper wire", "quantity": 5}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [tool])]))
    reply = assistant.turn("invalid-tax", "draft an invoice for Unknown customer for 5 copper wire with tax")
    assert reply[0]["text"] == resolve_text.table()["text"]["tax"]
    assert "processed" not in reply[0]["text"].lower()


def test_resolve_text_sentences_never_say_processed():
    def strings(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, str):
            yield value

    assert all("processed" not in sentence.lower() for sentence in strings(resolve_text.table()["text"]))
