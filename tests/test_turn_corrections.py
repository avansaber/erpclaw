"""Corrections by typing, and nothing approvable behind a question: while a card is pending, the latest owner message
that mentions a field supplies it and the new card sets the old one aside; a turn ending with a question, a refusal or
an answer sets the pending card aside with its own decision stored; a corrected card never commits. A reply naming no
choice asks the same question again; a control word is never a label."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_party
from erpclaw_sales import tables as t


def assistant(engine, *replies):
    return Assistant(engine, ScriptedModel(list(replies)))


def inv(customer, quantity, item="copper wire"):
    return call("create_invoice", customer=customer, items=[{"item": item, "quantity": quantity}])


def kinds(events) -> list[str]:
    return [e["type"] for e in events]


def line(card, name):
    return next(v for k, v in card["card"]["lines"] if k == name)


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def card_of(events):
    cards = [e for e in events if e["type"] == "card"]
    assert len(cards) == 1, events
    return cards[0]


# ---------- a corrected card never commits ----------

def test_a_corrected_card_never_commits_and_the_correction_commits_once(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [inv("Acme Corporation", 3)]))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", "actually make it 3")
    assert kinds(ev) == ["notice", "card"] and ev[0]["code"] == "card_set_aside", ev
    second = ev[1]
    assert second["replaces"] == first["approval_id"] and "3" in str(second["card"]["lines"]), second
    invoices = count(engine, t.invoice)
    old = approvals.approve(engine, first["approval_id"], first["digest"])
    assert old.status == "superseded" and count(engine, t.invoice) == invoices, old
    new = approvals.approve(engine, second["approval_id"], second["digest"])
    assert new.status == "committed" and count(engine, t.invoice) == invoices + 1, new
    again = approvals.approve(engine, second["approval_id"], second["digest"])
    assert again.status == "committed" and again.operation_key == new.operation_key  # read back, never twice
    assert count(engine, t.invoice) == invoices + 1


def test_the_corrected_card_approved_first_leaves_the_old_one_unapprovable(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [inv("Acme Corporation", 3)]))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    second = card_of(a.turn("c1", "actually make it 3"))
    invoices = count(engine, t.invoice)
    assert approvals.approve(engine, second["approval_id"], second["digest"]).status == "committed"
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "superseded"
    assert count(engine, t.invoice) == invoices + 1


def test_a_correction_ending_with_a_question_sets_the_card_aside_and_its_approval_returns_the_question(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [inv("Wayne", 5)]))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", "no, Wayne, not Acme")
    assert kinds(ev) == ["notice", "ask"] and ev[0]["code"] == "card_set_aside", ev
    assert "Wayne Enterprises" in ev[1]["options"], ev
    invoices = count(engine, t.invoice)
    out = approvals.approve(engine, first["approval_id"], first["digest"])
    assert out.status == "superseded" and out.text == ev[1]["text"] and out.data.get("code") == ev[1]["code"], out
    assert count(engine, t.invoice) == invoices


def test_a_refusal_after_a_card_sets_it_aside(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [call("get_metric", metric="sales")]))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", "and how much did we sell this year")
    assert ev[0]["type"] == "notice" and ev[0]["code"] == "card_set_aside", ev
    assert "Ask again to get it back." in ev[0]["text"], ev
    invoices = count(engine, t.invoice)
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "superseded"
    assert count(engine, t.invoice) == invoices


def test_a_read_answer_while_a_card_is_pending_sets_it_aside(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [call("get_metric", metric="receivables")]))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", "who owes us money?")
    assert kinds(ev) == ["notice", "answer"], ev
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "superseded"


def test_a_correction_the_model_ignores_is_asked_and_the_old_card_is_set_aside(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [inv("Acme Corporation", 5)]))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", "make it 7")
    assert kinds(ev) == ["notice", "ask"] and "7" in ev[1]["text"], ev  # the 7 is asked about, never dropped
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "superseded"


# ---------- the latest mention supplies the field ----------

def test_the_latest_customer_replaces_the_earlier_one_and_its_binding(engine):
    a = assistant(engine, Reply("", [inv("Wayne", 5)]), Reply("", [inv("Wayne Enterprises", 5)]),
                  Reply("", [inv("Hooli", 5)]))
    assert kinds(a.turn("c1", "Invoice Wayne for 5 copper wire")) == ["ask"]
    assert line(card_of(a.turn("c1", "yes")), "Customer").startswith("Wayne Enterprises")
    ev = a.turn("c1", "actually it's for Hooli")
    assert kinds(ev) == ["notice", "card"], ev
    assert line(ev[1], "Customer").startswith("Hooli"), ev[1]["card"]["lines"]


def test_the_latest_mention_of_each_field_supplies_it(engine):
    """The rule on the request's words (no switched-on write takes an owner's date yet, so the date case is shown here,
    on the words the resolution step reads): a later date, customer or document replaces every earlier mention of its
    kind; an earlier figure drops out only when the calls use the later one."""
    from erpclaw_assistant.resolve import MESSAGE_BREAK, request_words, words

    with engine.connect() as conn:
        ctx = company_ctx(conn)
        said = ["Invoice Acme Corporation for 5 copper wire yesterday", "make it 2 days ago"]
        ws = words(request_words(ctx, said, [(None, {"customer": "Acme Corporation"})]))
        assert "yesterday" not in ws and ws[-3:] == ["2", "days", "ago"] and MESSAGE_BREAK in ws, ws
        used = words(request_words(ctx, ["Invoice Acme Corporation for 5 copper wire", "make it 3"],
                                   [(None, {"customer": "Acme Corporation", "items": [{"quantity": 3}]})]))
        assert "5" not in used and "3" in used, used
        kept = words(request_words(ctx, ["Invoice Acme Corporation for 5 copper wire", "make it 3"],
                                   [(None, {"customer": "Acme Corporation", "items": [{"quantity": 5}]})]))
        assert "5" in kept and "3" in kept, kept  # a correction the call ignores stays, so it is asked about
        moved = words(request_words(ctx, ["Invoice Acme Corporation for 5 copper wire", "for Hooli"],
                                    [(None, {"customer": "Hooli"})]))
        assert "acme" not in moved and "hooli" in moved, moved


# ---------- answers ----------

def test_a_reply_naming_no_choice_asks_the_same_question_again(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [inv("Acme Corporation", 5)]))
    first = a.turn("c1", "Invoice either Acme Corporation or Hooli for 5 copper wire.")
    assert kinds(first) == ["ask"] and first[0]["code"] == "customer_ambiguous", first
    again = a.turn("c1", "Whichever is fine, just do it.")
    assert kinds(again) == ["ask"], again
    assert again[0]["text"] == first[0]["text"] and again[0]["options"] == first[0]["options"]
    assert again[0]["data"]["choices"] == first[0]["data"]["choices"]  # the same question, its codes still valid
    assert count(engine, m.approval) == 0 and store.bindings(store.open_request("c1")["id"]) == {}


def test_a_number_at_a_free_question_is_its_answer_never_a_position(engine):
    a = assistant(engine, Reply("", [call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire"}])]),
                  Reply("", [inv("Acme Corporation", 1)]))
    first = a.turn("c1", "Invoice Acme Corporation for copper wire")
    assert kinds(first) == ["ask"] and first[0]["code"] == "quantity_missing", first
    ev = a.turn("c1", "1")
    assert kinds(ev) == ["card"], ev
    shown = str(ev[0]["card"]["lines"])
    assert "28.00" in shown and "140.00" not in shown, shown  # one copper wire at its list price


def test_a_control_word_is_never_a_label_even_when_a_customer_bears_it(engine):
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", "Someone Else", "x@else.example", "Pune")
    a = assistant(engine, Reply("", [inv("Wayne", 5)]), Reply("", [inv("Wayne", 5)]))
    ask = a.turn("c1", "Invoice Wayne for 5 copper wire")
    assert kinds(ask) == ["ask"] and "someone else" in ask[0]["options"], ask
    ev = a.turn("c1", "someone else")
    assert kinds(ev) == ["ask"], ev  # the full question next, never a card for the customer "Someone Else"
    bound = store.bindings(store.open_request("c1")["id"])
    assert bound and all(v is None for v in bound.values()), bound


@pytest.mark.parametrize("said", ["cancel", "never mind"])
def test_cancel_with_a_card_pending_ends_the_request_and_declines_the_card(engine, said):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("OK."), Reply("", [inv("Hooli", 5)]))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", said)
    assert kinds(ev) == ["answer"] and ev[0]["text"] == "Cancelled; nothing was saved.", ev
    invoices = count(engine, t.invoice)
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "declined"
    assert count(engine, t.invoice) == invoices
    ev = a.turn("c1", "Invoice Hooli")  # a new request: the ended one's 5 copper wire never reaches it
    assert not any(e["type"] == "card" for e in ev), ev


# ---------- labels: exact first, a normalised label only when it is the one, positions ----------

def _traders(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "customer", "A&B Traders", "ab@traders.example", "Leeds")
        create_party(ctx, "customer", "AB Traders", "info@abtraders.example", "York")


def _answer_traders(engine, cid, said):
    a = assistant(engine, Reply("", [inv("Traders", 5)]), Reply("", [inv("Traders", 5)]))
    first = a.turn(cid, "Invoice Traders for 5 copper wire")
    assert kinds(first) == ["ask"] and {"A&B Traders", "AB Traders"} <= set(first[0]["options"]), first
    return first[0], a.turn(cid, said)


def test_a_label_typed_exactly_binds_its_own_choice_even_when_another_normalises_alike(engine):
    _traders(engine)
    _, ev = _answer_traders(engine, "c1", "A&B Traders")
    assert kinds(ev) == ["card"] and line(ev[0], "Customer").startswith("A&B Traders"), ev
    _, ev = _answer_traders(engine, "c2", "ab traders")  # exact as displayed, ignoring case
    assert kinds(ev) == ["card"] and line(ev[0], "Customer").startswith("AB Traders"), ev


def test_a_label_that_normalises_to_two_choices_binds_neither(engine):
    _traders(engine)
    _, ev = _answer_traders(engine, "c1", "A.B Traders")  # "a b traders" after normalising: both labels
    assert not any(e["type"] == "card" for e in ev), ev
    assert all(v is None for v in store.bindings(store.open_request("c1")["id"]).values())


def test_a_position_binds_that_choice(engine):
    _traders(engine)
    ask, ev = _answer_traders(engine, "c1", "2")
    second = ask["options"][1]
    assert kinds(ev) == ["card"] and line(ev[0], "Customer").startswith(second), (ask["options"], ev)


# ---------- a typed correction the model answers in text: the engine re-proposes the card itself ----------

def test_a_correction_answered_in_text_is_applied_by_the_engine_and_the_old_card_never_commits(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Got it, Wayne instead of Acme."))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", "no, Wayne, not Acme")
    assert ev[0]["type"] == "notice" and ev[0]["code"] == "card_set_aside", ev
    assert ev[-1]["type"] in ("ask", "card") and "Wayne Enterprises" in str(ev[-1]), ev
    assert not any(e.get("checked") == "engine" and "Approve the card" in e.get("text", "") for e in ev), ev
    invoices = count(engine, t.invoice)
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "superseded"
    assert count(engine, t.invoice) == invoices


def test_a_quantity_typed_while_the_model_answers_in_text_is_asked_about_and_the_card_set_aside(engine):
    # as when the model re-proposes the card unchanged: the 3 is asked about, never dropped, never a card for 5
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Sure, 3 it is."))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", "make it 3")
    assert kinds(ev) == ["notice", "ask"] and "3" in ev[1]["text"], ev
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "superseded"


def test_a_message_that_changes_nothing_sets_the_card_aside_and_says_so(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay, cancelled."))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", "cancel that")
    assert not any(e["type"] == "card" for e in ev), ev
    assert ev[-1]["text"] == ("I didn't change that card, so I've set it aside; nothing was saved. "
                              "Tell me what to change, or ask again to get it back."), ev
    out = approvals.approve(engine, first["approval_id"], first["digest"])
    assert out.status == "superseded", out


@pytest.mark.parametrize("said", ["thanks", "ok", "approve it", "yes", "Yes!", "yes please", "yeah", "sure",
                                  "yes approve the card", "yes, please", "sure.", "ok, thanks", "great, thank you",
                                  "yes thanks", "thanks!"])  # the last five: commas and full stops are spaces
def test_an_acknowledgement_or_an_approval_word_keeps_the_card_and_its_notice(engine, said):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("You're welcome."))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", said)
    assert kinds(ev) == ["answer"] and ev[0]["text"] == "Nothing has been saved yet. Approve the card to save it.", ev
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "committed"


@pytest.mark.parametrize("said", ["ok, but make it Wayne", "thanks, wrong customer though", "yes, but make it Wayne",
                                  "sure, 3 days", "yeah change the customer", "yes please, Acme",
                                  "please", "that card", "ok, please",  # filler is no acknowledgement
                                  "yes, Acme", "ok thanks, wrong customer", "thanks Acme",
                                  "approve it, 3", "yes approve it 3 please",  # a figure is a word too
                                  "approve 5", "approve INV-2026-00007"])  # set aside unchanged: it fails safe
def test_a_message_that_only_contains_an_acknowledgement_is_re_proposed(engine, said):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Sure."))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ev = a.turn("c1", said)
    assert not any("Approve the card" in e.get("text", "") for e in ev), ev
    invoices = count(engine, t.invoice)
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "superseded"
    assert count(engine, t.invoice) == invoices


@pytest.mark.parametrize("said, reply", [("approve it", "Approved."), ("yes approve the card", "Sure."),
                                         ("yes", "Done, I've saved it.")])
def test_a_covered_message_after_the_card_was_set_aside_saves_nothing_and_makes_no_card(engine, said, reply):
    # an approval word gets the engine's own reply; a bare acknowledgement goes to the model, whose claim is replaced
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Okay, cancelled."), Reply(reply))
    first = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    a.turn("c1", "cancel that")
    invoices = count(engine, t.invoice)
    ev = a.turn("c1", said)
    assert kinds(ev) == ["answer"] and ev[0]["text"] == "Nothing has been saved yet.", ev
    assert approvals.approve(engine, first["approval_id"], first["digest"]).status == "superseded"
    assert count(engine, t.invoice) == invoices



# ---------- the card a correction replaces must still be pending when the new card is made ----------

class ApprovesFirst:
    """A model that, while the turn waits for its reply, sees the owner approve the old card through the API."""

    def __init__(self, engine, reply):
        self.engine, self.reply, self.card = engine, reply, None
        self.first = ScriptedModel([Reply("", [inv("Acme Corporation", 5)])])

    def complete(self, messages, tools):
        if self.card is None:
            return self.first.complete(messages, tools)
        approvals.approve(self.engine, self.card["approval_id"], self.card["digest"])
        self.card, reply = False, self.reply
        return reply


@pytest.mark.parametrize("said, reply", [("actually make it 3", Reply("", [inv("Acme Corporation", 3)])),
                                         ("actually it's for Hooli", Reply("Sure, Hooli it is."))],
                         ids=["model call", "engine re-proposal"])
def test_a_correction_racing_the_old_card_s_approval_makes_no_second_card(engine, said, reply):
    model = ApprovesFirst(engine, reply)
    a = Assistant(engine, model)
    model.card = card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    invoices, cards = count(engine, t.invoice), count(engine, m.approval)
    ev = a.turn("c1", said)
    assert count(engine, t.invoice) == invoices + 1  # the old card's own commit, and only that
    assert count(engine, m.approval) == cards and not any(e["type"] == "card" for e in ev), ev
    assert any(e.get("code") == "replaced_card_ended" and "already approved" in e["text"] for e in ev), ev


def _approval_time_question(engine):
    """A payment card, then a second open invoice for its customer: approving it asks which invoice."""
    from erpclaw_core.plugins import REGISTRY

    pay = Reply("", [call("record_payment", customer="Initech", amount="100", method="check")])
    a = assistant(engine, pay, pay)
    card = card_of(a.turn("c1", "Initech paid 100 by check"))
    extra = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                              {"customer": "Initech", "items": [{"item": "copper wire", "quantity": 5}]})
    assert approvals.approve(engine, extra.approval_id, extra.digest).status == "committed"
    out = approvals.approve(engine, card["approval_id"], card["digest"])
    assert out.status == "superseded" and out.data.get("code") == "invoice_ambiguous", out  # a second open invoice
    return a, pay, out


@pytest.mark.parametrize("how", ["label", "code", "code after a restart"])
def test_an_approval_time_question_s_answer_continues_the_request(engine, how):
    a, pay, out = _approval_time_question(engine)
    if how == "label":  # the owner saw the question where they approved, as the approve endpoint notes it
        a.note("c1", out.text, question={"code": out.data["code"], "options": out.data.get("options") or [],
                                         "data": out.data.get("data") or {}})
        ev = a.turn("c1", "oldest first")  # the answer to the engine's own question, by its label
    else:
        shown = a.turn("c1", "hmm")  # the question is shown again, with its codes
        ask = next(e for e in shown if e["type"] == "ask" and e.get("code") == "invoice_ambiguous")
        code = next(c["code"] for c in ask["choices"] if c["label"] == "oldest first")
        if how == "code after a restart":
            a = assistant(engine, pay)  # a new process: the question and its codes come from the store
        ev = a.turn("c1", "oldest first", choice=code)
    assert ev and ev[-1]["type"] == "card", ev


@pytest.mark.parametrize("how", ["label", "code with its label"])
def test_an_answer_naming_the_customer_after_a_correction_binds_it(engine, how):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Got it, Wayne instead of Acme."),
                  Reply("", [inv("Wayne", 5)]))
    card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    ask = a.turn("c1", "no, Wayne, not Acme")[-1]
    assert ask["type"] == "ask" and "Wayne Enterprises" in ask["options"], ask
    code = next(c["code"] for c in ask["choices"] if c["label"] == "Wayne Enterprises")
    ev = a.turn("c1", "Wayne Enterprises", choice=code if how != "label" else None)
    assert kinds(ev) == ["card"] and line(ev[0], "Customer").startswith("Wayne Enterprises"), ev


@pytest.mark.parametrize("restart", [False, True], ids=["same process", "after a restart"])
def test_a_coded_question_recovered_after_its_turn_s_record_was_lost_never_reuses_its_codes(engine, monkeypatch,
                                                                                             restart):
    _traders(engine)
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Got it, Wayne instead of Acme."),
                  Reply("", [inv("Traders", 5)]))
    card_of(a.turn("c1", "Invoice Acme Corporation for 5 copper wire"))
    real = store.finalise
    monkeypatch.setattr(store, "finalise", lambda *x, **k: False)  # the books hold the set-aside, the store nothing
    a.turn("c1", "no, Wayne, not Acme")
    monkeypatch.setattr(store, "finalise", real)
    later = store._now  # the next message comes once that turn's claim has lapsed, and takes the conversation over
    monkeypatch.setattr(store, "_now", lambda: later() + store.LEASE + timedelta(seconds=1))
    if restart:  # a new process: everything it knows comes from the store and the books
        a = assistant(engine, Reply("", [inv("Traders", 5)]))
    old = next(e for e in a.turn("c1", "hmm") if e["type"] == "ask")  # recovered, with the codes it was asked with
    a.turn("c1", "cancel")
    new = next(e for e in a.turn("c1", "Invoice Traders for 5 copper wire") if e["type"] == "ask")
    assert new["question_id"] != old["question_id"], (old, new)
    ev = a.turn("c1", "that one", choice=old["choices"][0]["code"])  # a stale code binds nothing
    assert not any(e["type"] == "card" for e in ev), ev



# ---------- an invoice date the engine cannot set yet: asked, never a card dated otherwise without asking ----------

DATED = "Invoice Acme Corporation for 5 copper wire dated 05/10/2026"


def _date_question(engine, *more):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), *more)
    ev = a.turn("c1", DATED)
    return a, ev


def test_a_date_the_engine_cannot_set_is_asked_about_with_two_coded_choices_and_no_card(engine):
    cards = count(engine, m.approval)
    _, ev = _date_question(engine)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "date_choice_missing", ev
    assert ev[0]["options"] == ["Date it today", "Leave it"] and len(ev[0]["choices"]) == 2, ev
    assert ev[0]["text"].startswith("Invoices are dated today; choosing an invoice date isn't available yet."), ev
    assert count(engine, m.approval) == cards


@pytest.mark.parametrize("by", ["label", "code"])
def test_date_it_today_makes_the_card_dated_today(engine, by):
    a, ev = _date_question(engine, Reply("", [inv("Acme Corporation", 5)]))
    code = next(c["code"] for c in ev[0]["choices"] if c["label"] == "Date it today")
    ev = a.turn("c1", "Date it today", choice=code if by == "code" else None)
    card = card_of(ev)
    assert line(card, "Date") == "26 Oct 2026", card


def test_leave_it_ends_the_request_and_saves_nothing(engine):
    cards, invoices = count(engine, m.approval), count(engine, t.invoice)
    a, _ = _date_question(engine, Reply("", [inv("Acme Corporation", 5)]))
    ev = a.turn("c1", "Leave it")
    assert ev == [{"currency": "USD", "type": "answer", "text": "Cancelled; nothing was saved.", "checked": "engine"}], ev
    assert (count(engine, m.approval), count(engine, t.invoice)) == (cards, invoices)
    assert store.open_request("c1") is None


def test_a_date_that_is_today_is_never_the_date_question(engine):
    # today is the card's own date, so nothing is left undone; the date phrase is still asked about where it can't be
    # placed on the card (as before), never answered by dating the invoice otherwise
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]))
    ev = a.turn("c1", "Invoice Acme Corporation for 5 copper wire dated 26/10/2026")
    assert not any(e.get("code") == "date_choice_missing" or e["type"] == "not_done" for e in ev), ev
