"""Every write card states, inside the card the owner approves, its date and where that date came from: the owner's
own words, quoted, or the default, or that the operation takes no date. The engine sets and checks it; the model never
can."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from erpclaw_api.app import create_app
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall, call
from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import Refusal
from erpclaw_core.plugins import REGISTRY, disable_plugin, install_plugin

DEFAULT_LINE = ["Date source", "No date was read from your words; the card uses its default date."]
DRAFT_LINE = ["Date source", "No date applies: cancelling a draft changes no books."]
PAY = {"customer": "Hooli", "amount": "100"}


def kinds(events) -> list[str]:
    return [e["type"] for e in events]


def card_of(engine, said: str, *calls, answers=()) -> dict:
    assistant = Assistant(engine, ScriptedModel([Reply("", list(calls)) for _ in range(len(answers) + 1)]))
    conversation = uuid.uuid4().hex
    ev = assistant.turn(conversation, said)  # its own request
    for label in answers:
        assert not any(e["type"] == "card" for e in ev), ev
        [question] = [e for e in ev if e["type"] == "ask"]
        [choice] = [c for c in question["choices"] if c["label"] == label]
        ev = assistant.turn(conversation, label, choice=choice["code"])
    cards = [e for e in ev if e["type"] == "card"]
    assert len(cards) == 1, ev
    return cards[0]


def count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(m.approval)).scalar_one()


# ---------- the three forms ----------

def test_a_date_read_from_the_owner_s_words_is_stated_with_its_phrase(engine):
    c = card_of(engine, "Hooli paid 100 twenty-one days ago", call("record_payment", customer="Hooli", amount=100))
    assert c["card"]["date_source"] == {"phrase": "twenty-one days ago", "date": "2026-10-05"}
    assert c["card"]["lines"][-1] == ["Date source", "From your words: 'twenty-one days ago' (05 Oct 2026)"]


def test_a_default_date_is_stated_as_the_default(engine):
    c = card_of(engine, "Hooli paid 100", call("record_payment", customer="Hooli", amount=100))
    assert c["card"]["date_source"] == {"default": True} and c["card"]["lines"][-1] == DEFAULT_LINE


def test_an_invoice_with_a_date_written_is_stated_as_its_default_beside_its_notice(engine):
    invoice = Reply("", [call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])])
    a = Assistant(engine, ScriptedModel([invoice, invoice]))
    ask = a.turn("inv", "tomorrow invoice Hooli for 5 copper wire")
    assert [e["code"] for e in ask] == ["date_choice_missing"], ask
    assert ask[0]["type"] == "ask" and ask[0]["text"] == (
        "An invoice can't be dated in the future. Shall I date it today, or leave it?")
    assert [c["label"] for c in ask[0]["choices"]] == ["Date it today", "Leave it"]


def test_cancelling_a_posted_invoice_states_its_reversal_date_and_source(engine):
    c = card_of(engine, "cancel INV-2026-00003 yesterday", call("cancel_invoice", invoice="INV-2026-00003"))
    assert c["card"]["date_source"] == {"phrase": "yesterday", "date": "2026-10-25"}
    assert any("25 Oct 2026" in v for k, v in c["card"]["lines"] if k != "Date source"), c["card"]["lines"]
    c = card_of(engine, "cancel INV-2026-00003", call("cancel_invoice", invoice="INV-2026-00003"))
    assert c["card"]["date_source"] == {"default": True}


def test_cancelling_a_draft_states_that_no_date_applies(engine):
    c = card_of(engine, "cancel INV-2026-00008", call("cancel_invoice", invoice="INV-2026-00008"))
    assert c["card"]["date_source"] == {"not_applicable": True} and c["card"]["lines"][-1] == DRAFT_LINE
    ev = Assistant(engine, ScriptedModel([Reply("", [call("cancel_invoice", invoice="INV-2026-00008")])])).turn(
        "dd", "cancel INV-2026-00008 yesterday")
    assert any(e["type"] == "not_done" and e.get("part") == "date" for e in ev), ev  # never silently ignored
    assert next(e for e in ev if e["type"] == "card")["card"]["date_source"] == {"not_applicable": True}


# ---------- coverage ----------

def test_every_switched_on_write_drafted_by_the_assistant_states_its_date_source(engine, monkeypatch):
    from decimal import Decimal

    from erpclaw_core.common import company_ctx
    from erpclaw_sales import service as s
    from erpclaw_sales import tables as t

    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
    install_plugin(engine, "email")
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        r = s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal("100"), ctx.today, oldest_first=True))
        number = conn.execute(select(t.payment.c.number).where(t.payment.c.id == r["id"])).scalar_one()
    from test_supplier_payment_reversal_contract import supplier_reversal_scenario, delivery_reversal_scenario

    supplier_args = {"payment": "SPAY-2026-00001"}
    delivery_args = {"delivery": "GR-2026-00001"}
    reversal_scenarios = {"reverse_supplier_payment": supplier_reversal_scenario,
                          "reverse_goods_received": delivery_reversal_scenario}
    drafted = {
        "update_email_sender": ("Update email sender settings for Company Books",
                                call("update_email_sender", from_address="books@example.test",
                                     display_name="Company Books", smtp_host="smtp.example.test", port=587,
                                     security="starttls", username="books", reply_to="reply@example.test")),
        "add_customer": ("add a customer called Lakeside Cafe",
                         ToolCall("customer", "add_customer", {"name": "Lakeside Cafe"})),
        "add_supplier": ("add a supplier called Copperline Metals",
                         ToolCall("supplier", "add_supplier", {"name": "Copperline Metals"})),
        "add_cash_account": ("add a cash account",
                             ToolCall("cash", "add_cash_account", {})),
        "record_opening_position": ("Record an opening position with 5.00 in Bank",
                                    call("record_opening_position", bank_lines=[{"account": "Bank", "amount": "5.00"}])),
        "add_item": ("add an item called Bookkeeping, sold by the hour at 80",
                     ToolCall("item", "add_item", {"name": "Bookkeeping", "unit": "hour", "price": "80"})),
        "create_invoice": ("invoice Hooli for 5 copper wire",
                           call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])),
        "submit_invoice": ("submit INV-2026-00008", call("submit_invoice", invoice="INV-2026-00008")),
        "record_payment": ("Hooli paid 100", call("record_payment", customer="Hooli", amount=100)),
        "cancel_invoice": ("cancel INV-2026-00003", call("cancel_invoice", invoice="INV-2026-00003")),
        "reverse_payment": (f"reverse {number}", call("reverse_payment", payment=number)),
        "reverse_supplier_payment": ("reverse supplier payment SPAY-2026-00001", call("reverse_supplier_payment", **supplier_args)),
        "reverse_goods_received": ("reverse goods received GR-2026-00001", call("reverse_goods_received", **delivery_args)),
    }
    from core_writes.scenarios import resend_message

    resend_args, resend_words = resend_message(None, engine)
    drafted["resend_message"] = (resend_words, call("resend_message", **resend_args))
    # These sales and stock writes ship switched off; the step that switches them on adds them to
    # `drafted`, drafted by the assistant, rather than pinning them here.
    for name in ("create_sales_order", "deliver_order", "create_credit_note", "record_opening_stock",
                 "write_off_stock", "record_stock_count", "cancel_stock_movement"):
        monkeypatch.setattr(REGISTRY.contracts[name], "planned", "Core")
    writes = {n for n, c in REGISTRY.contracts.items() if c.effect != "read" and not c.planned}
    # Setup refuses these ready demo books; its date source is checked on pending books in
    # test_company_setup_drafted_on_pending_books_states_that_no_date_applies below.
    assert writes == set(drafted) | {"set_up_company"}, writes ^ (set(drafted) | {"set_up_company"})
    for name in sorted(writes - {"set_up_company"}):
        if name in reversal_scenarios:
            reversal_scenarios[name](engine)
        said, model_call = drafted[name]
        c = card_of(engine, said, model_call, answers=("A service I sell",) if name == "add_item" else ())
        assert c["card"].get("date_source") and c["card"]["lines"][-1][0] == "Date source", (name, c["card"])
        if name in {"add_customer", "add_item"}:
            reason = "adding a customer" if name == "add_customer" else "adding an item"
            assert c["card"]["date_source"] == {"not_applicable": True}
            assert c["card"]["lines"][-1] == ["Date source", f"No date applies: {reason} posts nothing."]
        elif name in {"add_supplier", "update_email_sender", "add_cash_account", "resend_message"}:
            reason = REGISTRY.contracts[name].no_date_reason["en"]
            assert c["card"]["date_source"] == {"not_applicable": True}
            assert c["card"]["lines"][-1] == ["Date source", f"No date applies: {reason}"]


def test_company_setup_drafted_on_pending_books_states_that_no_date_applies(engine):
    from conftest import drop_everything
    from erpclaw_core import setup

    drop_everything(engine)
    company = setup.create_own_books(engine)
    c = card_of(engine, "Set up my books as Lakeside Bakery in US dollars",
                ToolCall("setup", "set_up_company", {"name": "Lakeside Bakery", "currency": "US dollars"}),
                answers=("January", "03/04 is 3 April (day first)"))
    assert c["card"]["title"] == "Set up your books"
    assert c["card"]["date_source"] == {"not_applicable": True}
    assert c["card"]["lines"][-1] == ["Date source", "No date applies: setting up your books posts nothing."]
    with engine.connect() as conn:
        assert conn.execute(select(m.company.c.status).where(m.company.c.id == company)).scalar_one() == "pending"
        assert conn.execute(select(func.count()).select_from(m.ledger_entry)).scalar_one() == 0


def test_a_card_proposed_without_the_assistant_has_no_date_source(engine):
    card = approvals.propose(engine, REGISTRY.contracts["record_payment"], PAY)
    assert "date_source" not in card.card and all(k != "Date source" for k, _ in card.card["lines"])


# ---------- validation by the engine ----------

@pytest.mark.parametrize("source, words", [
    ({"default": True, "extra": 1}, "Hooli paid 100"),
    ({"default": False}, "Hooli paid 100"),
    ({"phrase": "yesterday", "date": "2026-10-25"}, "Hooli paid 100"),  # not in the owner's words
    ({"phrase": "y" * 81, "date": "2026-10-25"}, "Hooli paid 100 " + "y" * 81),
    ({"phrase": "yesterday", "date": "25/10/2026"}, "Hooli paid 100 yesterday"),
    ({"phrase": "yesterday", "date": "2026-10-24"}, "Hooli paid 100 yesterday"),  # not the card's own date
    ({"not_applicable": True}, "Hooli paid 100"),  # only a draft's cancellation takes no date
    ({}, "Hooli paid 100"),
])
def test_a_malformed_date_source_refuses_and_makes_no_card(engine, source, words):
    before = count(engine)
    args = {**PAY, "date": "2026-10-25"} if "date" in source else PAY
    with pytest.raises(Refusal) as r:
        approvals.propose(engine, REGISTRY.contracts["record_payment"], args, date_source=source, owner_words=words)
    assert r.value.code == "date_source_invalid" and count(engine) == before


def test_the_model_cannot_set_a_date_source(engine):
    c = card_of(engine, "Hooli paid 100", call("record_payment", customer="Hooli", amount=100,
                                               date_source={"phrase": "Hooli", "date": "2026-10-26"}))
    assert c["card"]["date_source"] == {"default": True}


# ---------- the digest, recomputation, placement and markup ----------

def test_the_digest_covers_the_date_source_and_approval_reuses_the_stored_one(engine):
    contract = REGISTRY.contracts["record_payment"]
    a = approvals.propose(engine, contract, {**PAY, "date": "2026-10-25"},
                          date_source={"phrase": "yesterday", "date": "2026-10-25"}, owner_words="Hooli paid 100 yesterday")
    b = approvals.propose(engine, contract, {**PAY, "date": "2026-10-25"},
                          date_source={"phrase": "Yesterday", "date": "2026-10-25"}, owner_words="Yesterday Hooli paid 100")
    assert a.digest != b.digest
    with pytest.raises(Refusal) as r:  # the digest of another card's date source
        approvals.approve(engine, a.approval_id, b.digest)
    assert r.value.code == "digest"
    out = approvals.approve(engine, b.approval_id, b.digest)  # recomputed from the stored date source: unchanged
    assert out.status == "committed", out


def test_the_line_is_last_and_carries_no_amount(engine):
    c = card_of(engine, "Hooli paid 100 yesterday", call("record_payment", customer="Hooli", amount=100))
    last = len(c["card"]["lines"]) - 1
    assert c["card"]["lines"][last][0] == "Date source"
    assert all(not (a["where"] == "lines" and a["index"] == last) for a in c["card"]["amounts"])


def test_the_phrase_is_rendered_as_quoted_data(engine):
    contract = REGISTRY.contracts["record_payment"]
    said = """Hooli paid 100 on 25 Oct "<b>it's</b>" """
    card = approvals.propose(engine, contract, {**PAY, "date": "2026-10-25"},
                             date_source={"phrase": "25 Oct \"<b>it's</b>\"", "date": "2026-10-25"}, owner_words=said)
    line = card.card["lines"][-1][1]
    assert line.startswith("From your words: '") and "<b>" in line and "it\\'s" in line, line


# ---------- the API ----------

def test_the_api_card_and_its_read_back_carry_the_date_source(engine):
    c = TestClient(create_app(engine, ScriptedModel([Reply("", [call("record_payment", customer="Hooli",
                                                                     amount=100)])]), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    [card] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "Hooli paid 100 yesterday"}).json()["events"]
    assert card["card"]["date_source"] == {"phrase": "yesterday", "date": "2026-10-25"}
    assert card["card"]["lines"][-1][0] == "Date source"


# ---------- a card whose operation no longer takes, or now takes, a date ----------

def test_a_draft_s_cancellation_whose_draft_was_submitted_meanwhile_never_commits_with_its_old_source(engine):
    """The draft is submitted while its cancellation card is pending: approving that card must not reverse the posted
    invoice under a line saying no date applies and no books change."""
    from sqlalchemy import select as sel

    from erpclaw_sales import tables as t

    cancel = card_of(engine, "cancel INV-2026-00008", call("cancel_invoice", invoice="INV-2026-00008"))
    assert cancel["card"]["date_source"] == {"not_applicable": True}
    submit = approvals.propose(engine, REGISTRY.contracts["submit_invoice"], {"invoice": "INV-2026-00008"})
    assert approvals.approve(engine, submit.approval_id, submit.digest).status == "committed"
    with engine.connect() as conn:
        before = conn.execute(sel(func.count()).select_from(m.ledger_entry)).scalar_one()
    out = approvals.approve(engine, cancel["approval_id"], cancel["digest"], ownership_check=_ownership(engine))
    assert out.status != "committed" and out.card is None, out  # ended, and no fresh card at all
    assert out.data["code"] == "document_changed" and approvals.DOCUMENT_CHANGED in out.text, out
    with engine.connect() as conn:
        assert conn.execute(sel(func.count()).select_from(m.ledger_entry)).scalar_one() == before
        assert conn.execute(sel(t.invoice.c.status).where(t.invoice.c.number == "INV-2026-00008")).scalar_one() \
            != "cancelled"


def test_a_card_whose_operation_now_takes_no_date_is_refused(engine, monkeypatch):
    """The reverse, forced: a posted invoice's cancellation card, then the input says it takes no date."""
    from erpclaw_sales import CancelInvoice

    cancel = card_of(engine, "cancel INV-2026-00003", call("cancel_invoice", invoice="INV-2026-00003"))
    assert cancel["card"]["date_source"] == {"default": True}
    monkeypatch.setattr(CancelInvoice, "takes_no_date", lambda self, ctx: True)
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(m.ledger_entry)).scalar_one()
    out = approvals.approve(engine, cancel["approval_id"], cancel["digest"], ownership_check=_ownership(engine))
    assert out.status != "committed" and out.card is None, out
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.ledger_entry)).scalar_one() == before


def test_a_posted_invoice_s_cancellation_with_its_books_unchanged_commits(engine):
    cancel = card_of(engine, "cancel INV-2026-00003 yesterday", call("cancel_invoice", invoice="INV-2026-00003"))
    out = approvals.approve(engine, cancel["approval_id"], cancel["digest"], ownership_check=_ownership(engine))
    assert out.status == "committed", out


def test_a_fresh_card_on_the_stale_path_never_inherits_an_unchecked_date_source(engine):
    """The books change under a payment card (another payment), so approval makes a fresh card: its date source is
    checked against the books now and still names the date the fresh card carries."""
    from decimal import Decimal

    from erpclaw_core.common import company_ctx
    from erpclaw_sales import service as s

    card = card_of(engine, "Pied Piper paid 500 yesterday, apply it oldest first",
                   call("record_payment", customer="Pied Piper", amount=500, oldest_first=True))
    assert card["card"]["date_source"] == {"phrase": "yesterday", "date": "2026-10-25"}
    with engine.begin() as conn:  # another payment to the oldest invoice changes what this card would apply
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == "Pied Piper")).mappings().one())
        s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal("100"), ctx.today, oldest_first=True))
    out = approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=_ownership(engine))
    assert out.status == "superseded" and out.card, out  # a fresh card
    assert out.card["date_source"] == {"phrase": "yesterday", "date": "2026-10-25"}, out.card
    assert out.card["lines"][-1] == ["Date source", "From your words: 'yesterday' (25 Oct 2026)"]


def test_a_default_date_shown_before_midnight_never_commits_as_the_next_day(engine, monkeypatch):
    """A card dated by default on the 26th, approved on the 27th: nothing is saved under the old card; a fresh card
    shows the new date, still by default, for the owner to approve."""
    card = card_of(engine, "Hooli paid 100", call("record_payment", customer="Hooli", amount=100))
    assert card["card"]["date_source"] == {"default": True}
    assert ["Date", "Received 26 Oct 2026"] in card["card"]["lines"]
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(m.ledger_entry)).scalar_one()
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-10-27")  # midnight passes between showing and approving
    out = approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=_ownership(engine))
    assert out.status != "committed", out
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.ledger_entry)).scalar_one() == before
    assert out.status == "superseded" and out.card is not None, out  # a fresh card, never the old one committed
    assert ["Date", "Received 27 Oct 2026"] in out.card["lines"] and out.card["date_source"] == {"default": True}


def _ownership(engine):
    from erpclaw_assistant import store
    from erpclaw_core import principals

    with engine.connect() as conn:
        owner = principals.owner(conn)["id"]
    return lambda cid: store.conversation_owner(cid) == owner
