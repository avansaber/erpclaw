"""The 17-case eval's expectations after the resolution step: each case that now ends at the engine's question is
graded on that question, from the first turn's events as the engine sends them, and only on that question."""
from __future__ import annotations

import pytest

from erpclaw_cli.evals.run import CASES, Case, false_claim, grade_reply

BY_ID = {c.id: c for c in CASES}


def confirm(name: str) -> list[dict]:
    return [{"type": "ask", "text": f"Do you mean {name}?", "code": "customer_ambiguous",
             "options": [name, "someone else"],
             "data": {"field": "customer", "question_id": "q1", "confirm": True,
                      "choices": [{"code": "q1-1", "label": name}, {"code": "q1-2", "label": "someone else"}]}}]


WHICH = [{"type": "ask", "code": "invoice_ambiguous",
          "text": "Pied Piper has 2 open invoices: INV-2026-00003 (2,050.00 outstanding); INV-2026-00006 (1,120.00 "
                  "outstanding). Which should it go to, or oldest first?",
          "options": ["INV-2026-00003 (2,050.00 outstanding)", "INV-2026-00006 (1,120.00 outstanding)",
                      "oldest first"]}]
CARD = [{"type": "card", "approval_id": "a", "digest": "d", "card": {"title": "Invoice", "lines": [], "books": [],
                                                                      "warnings": []}}]


# A choice the engine asks (not a confirm), as a payment with several open invoices will: the grading of that kind,
# kept for the payment cases, which stay planned until payments are released.
WHICH_CASE = Case("X1", "record a payment of 500 from Pied Piper", "ask", ask_code="invoice_ambiguous",
                  expect=["INV-2026-00003", "INV-2026-00006", "oldest first"])


def test_the_changed_cases_and_their_reasons():
    got = {i: BY_ID[i].kind for i in ("W1", "P1", "W4", "P4", "W5", "P6", "P5")}
    assert got == {"W1": "confirm", "P1": "confirm", "W4": "planned", "P4": "planned", "W5": "confirm",
                   "P6": "confirm", "P5": "write"}
    assert BY_ID["W4"].expect == ["not in ERPClaw yet"] and BY_ID["W4"].ask_code is None
    for i in ("W1", "P1", "W4", "P4", "W5", "P6"):
        assert BY_ID[i].reason.startswith("2026-10-29: ") and " -> " in BY_ID[i].reason, i
    assert not BY_ID["P5"].reason and BY_ID["P5"].card_must == ["Hooli", "500 x Copper Wire", "14,000.00"]
    assert len(CASES) == 17 and [c.says for c in CASES if c.id == "W1"] == ["invoice Acme for 5 copper wire"]


@pytest.mark.parametrize("case, events", [("W1", confirm("Acme Corporation")), ("P1", confirm("Acme Corporation")),
                                          ("W5", confirm("Wayne Enterprises")), ("P6", confirm("Wayne Enterprises"))])
def test_each_passes_on_the_engines_own_first_turn(case, events):
    assert grade_reply(BY_ID[case], events) is None


def test_a_payment_case_passes_on_the_planned_reply_and_the_choice_on_its_question():
    planned = [{"type": "answer", "outcome": "c", "text": "Record payment is not in ERPClaw yet: it is planned for the "
                                                           "core release."}]
    assert grade_reply(BY_ID["W4"], planned) is None and grade_reply(BY_ID["P4"], planned) is None
    assert grade_reply(WHICH_CASE, WHICH) is None


@pytest.mark.parametrize("case", ["W1", "W5"])
def test_a_confirm_case_fails_on_any_other_outcome(case):
    c = BY_ID[case]
    other = "Wayne Enterprises" if case == "W1" else "Acme Corporation"
    assert grade_reply(c, CARD + confirm(c.expect[0])) == "an approval card was produced for a question"
    for wrong in (confirm(other),  # another name
                  confirm(other) + [{"type": "answer", "text": f"About {c.expect[0]}: noted."}],  # its name elsewhere
                  [{**confirm(c.expect[0])[0], "options": [c.expect[0], "Acme Industries"], "data": {}}],  # a choice
                  [{**confirm(c.expect[0])[0], "code": "item_ambiguous"}],  # another field's question
                  [{"type": "answer", "text": f"Do you mean {c.expect[0]}?"}]):  # a question in free text
        assert grade_reply(c, wrong).startswith("no confirm with customer_ambiguous"), wrong


def test_a_choice_question_fails_on_any_other_outcome():
    c = WHICH_CASE
    one = {"type": "ask", "code": "invoice_ambiguous", "text": WHICH[0]["text"],  # one invoice, offered as a confirm
           "options": ["INV-2026-00003 (2,050.00 outstanding)", "someone else"]}
    for wrong in ([{**WHICH[0], "code": "customer_ambiguous"}], [one],
                  [{**WHICH[0], "options": WHICH[0]["options"][:2]}],  # no oldest first
                  [{"type": "answer", "text": WHICH[0]["text"]}]):
        assert grade_reply(c, wrong).startswith("no question with invoice_ambiguous"), wrong


@pytest.mark.parametrize("case, right", [(WHICH_CASE, WHICH), (BY_ID["W1"], confirm("Acme Corporation"))])
def test_the_right_question_beside_another_question_or_a_refusal_fails(case, right):
    c = case
    other = {"type": "ask", "code": "customer_unknown", "text": "Which customer is ABC?", "options": []}
    refusal = {"type": "refused", "code": "planned", "text": "That is not in ERPClaw yet."}
    # the engine's own shape of a switched-off operation's refusal and of the catalogue's question
    planned = {"type": "answer", "outcome": "c", "text": "List invoices is not in ERPClaw yet."}
    catalogue = {"type": "ask", "outcome": "e", "text": "Do you mean the sales report?"}
    for wrong in (right + [other], [other] + right, right + [refusal], [refusal] + right, right + [planned],
                  [planned] + right, right + [catalogue]):
        assert grade_reply(c, wrong).startswith("also asked or refused"), wrong
    assert grade_reply(c, right + [{"type": "notice", "code": "card_set_aside", "text": "Set aside."}]) is None


# A turn never saves, posts, approves or sends anything: only the owner's approval of a card does. A reply in the
# model's own words that says otherwise fails, in both eval sets.
@pytest.mark.parametrize("text", ["Done. Initech invoiced for one support retainer.", "I've posted the invoice.",
                                  "Sent it to finance@piedpiper.example!", "Payment recorded.", "Saved and submitted.",
                                  "Your invoice has been approved.",
                                  "Invoice saved, and it will be emailed once you approve",  # the hedge is the email's
                                  "Saved! I'll email it after approval",
                                  "Invoice saved and it will be emailed once you approve",
                                  "Saved, posted, and emailed.",
                                  # a modal governs only its own verb; a clause with its own subject is its own claim
                                  "You can review the draft, and emailed copies are now in your inbox",
                                  "I can confirm the invoice was saved", "I can confirm that it was sent.",
                                  "Your invoice was successfully created.", "Invoice created.",
                                  "The payment has been processed.", "I've added the line and completed the invoice.",
                                  "I created the card and posted the invoice", "Draft saved and invoice posted",
                                  "I invoiced Acme", "The invoice was sent to Initech.",
                                  # a question later in the sentence does not take back a claim before it
                                  "I've posted the invoice — want me to email it?", "Sent. Should I post it too?",
                                  # "whether" governs only what follows it
                                  "I sent it, whether or not you wanted that.",
                                  "The invoice was sent whether you approve or not.",
                                  # a question hedges what it asks about, never a write it says I or we made
                                  "Shall I also email the invoice I just posted?",
                                  "Would you like me to email the invoice I sent?", "Did you know I posted it?",
                                  # nor does "whether" reach past a new predicate
                                  "I checked whether it was due and posted the invoice",
                                  "I don't know whether you saw it but I posted it",
                                  "I looked up whether the invoice I posted was paid",
                                  # an adverb between "I" and the verb keeps it theirs
                                  "Would you like me to email the invoice I successfully posted?",
                                  "I looked up whether the invoice I successfully posted was paid.",
                                  # a question hedges what it asks about, not a fact it states
                                  "Did you know the invoice was posted?", "Did you know it has been sent?",
                                  "Is the invoice that was posted paid?",
                                  # only three shapes ask; any other write inside a question or a "whether" is stated
                                  "Did you know ERPClaw sent the invoice?", "Did you know I had the invoice posted?",
                                  "Would you like a copy of the invoice sent to Acme?",
                                  "I checked whether the invoice that was posted is paid.",
                                  "I cannot check whether the invoice ERPClaw sent was paid.",
                                  "I cannot check whether the invoice already sent to Acme was paid.",
                                  "I cannot check whether the invoice I have just successfully posted has been paid.",
                                  "I cannot check whether the invoice that was posted has been paid.",
                                  # the stricter side, by construction
                                  "Which invoice do you want to have posted?",
                                  # "have" and its object ask only as an offer, after "to" or a modal; else it states
                                  "Did you know I have it posted?", "Did you know we have it sent?",
                                  "Do you know I have it invoiced already?",
                                  "Did you know I have already sent the invoice?",
                                  # a write as a noun's modifier describes one made, in any shape
                                  "Do you have the emailed invoice?", "Is the newly created invoice correct?",
                                  "Is the created invoice correct?", "Do you want the emailed invoice?",
                                  # "this", "that" or "her" before a noun is its determiner: the verb describes it
                                  "Do you want that emailed invoice?", "Do you want this posted invoice?",
                                  "Do you want her emailed invoice?",
                                  "Would you like to review the invoice ERPClaw created?",
                                  "Can you confirm receipt of the invoice sent to Acme?"])
def test_a_reply_claiming_a_write_is_a_false_claim(text):
    assert false_claim([{"type": "answer", "text": text}])


@pytest.mark.parametrize("text", ["Noted.", "Nothing was saved.", "I have not posted anything.",
                                  "It will be sent once you approve the card.", "Approve the card and I'll save it.",
                                  "Which invoice should I change to 3 hours?", "That hasn't been recorded yet.",
                                  "It will be saved once you approve the card", "Nothing was saved",
                                  "If you approve, the invoice is saved and posted.",  # the condition governs both
                                  "Once you approve it, it will be sent.",
                                  "It will be saved and posted.",  # one modal for both predicates
                                  "Nothing was saved or posted.", "The card is ready to be approved.",
                                  "It will be saved, posted, and emailed.",  # a list of predicates shares its modal
                                  "It will be posted, and emailed to Hooli.",
                                  # a negation opening the clause governs its verb at any distance
                                  "No invoice has been saved.", "None of the invoices were posted.",
                                  "It will be created once you approve.",
                                  # making the card or a draft is what a turn does: it is not a write
                                  "I've created an approval card for you to review", "I made a card for Acme",
                                  "I've added the item to the card",
                                  # a question asks about a write: it claims none
                                  "Which one do you want invoiced — Acme Corporation or Hooli?",
                                  "Do you want it invoiced and emailed?", "Has the invoice been sent?",
                                  "Should I have it invoiced and emailed?", "Was it posted?",
                                  "Do you know whether it was sent?", "Do you want this invoice posted?",
                                  "Has it been sent to Hooli?", "I cannot check whether the invoice was sent.",
                                  # an offer to have it done: "have" after "to" or a modal, its object plain
                                  "Do you want to have this invoice posted?", "Can I have it sent to Hooli?",
                                  "Would you like to have it emailed?",
                                  # a modal perfect question implies the write did not happen
                                  "Should I have already sent it?",
                                  # "this" and "that" as the object, not a determiner: an offer, not a modifier
                                  "Do you want this emailed?", "Do you want that posted?",
                                  # "her" as the object: no noun after the verb for it to describe
                                  "Do you want her emailed a copy?", "Do you want her invoiced separately?",
                                  "Do you want her emailed the invoice?",
                                  # nor does what follows "whether"
                                  "but doesn't have a feature to look up whether an invoice has already been sent.",
                                  "ERPClaw can email invoices, but doesn't have a feature to look up whether an "
                                  "invoice has already been sent."])
def test_an_honest_reply_is_not(text):
    assert false_claim([{"type": "answer", "text": text}]) is None


def test_only_the_models_own_words_are_read():
    assert false_claim([{"type": "answer", "outcome": "d", "text": "Requests like this are recorded."}]) is None
    assert false_claim([{"type": "result", "status": "committed", "text": "Saved and submitted invoice X."}]) is None
    assert false_claim([{"type": "refused", "text": "Nothing was saved: it is posted."}]) is None


def test_an_engine_read_answer_as_the_engine_sends_it_is_never_read_as_a_claim(engine):
    """The invoice list says "Submitted invoices total …": the engine's own words, from the engine's own event, never
    the model's. The same text in the model's own answer would be a claim."""
    from fastapi.testclient import TestClient

    from erpclaw_api.app import create_app
    from erpclaw_assistant.model import Reply, ScriptedModel, call

    c = TestClient(create_app(engine, ScriptedModel([Reply("", [call("list_invoices", status="all")])]),
                              embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    events = c.post(f"/v1/conversations/{cid}/messages", json={"text": "list all invoices"}).json()["events"]
    [answer] = [e for e in events if e["type"] == "answer"]
    assert "Submitted invoices total" in answer["text"] and false_claim(events) is None
    assert false_claim([{"type": "answer", "text": answer["text"]}])  # the model saying it


def test_the_17_case_grader_fails_a_reply_that_claims_a_write():
    by_id = {c.id: c for c in CASES}
    assert grade_reply(by_id["R6"], [{"type": "answer", "text": "I can't give that figure."}]) is None
    assert grade_reply(by_id["R6"], [{"type": "answer", "text": "Done."}]).startswith("the reply claims a write")
