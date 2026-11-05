"""The turn's contract on what reaches the owner: a "none" pick never denies a capability the owner named, a reply
never claims a change the turn did not commit, and a customer the owner rules out is never a claimant."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import catalogue as cat
from erpclaw_core import models as m
from erpclaw_core.plugins import disable_plugin
from erpclaw_sales import tables as t


def client(engine, replies):
    c = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    return c, c.post("/v1/conversations").json()["id"]


def say(c, cid, said):
    return c.post(f"/v1/conversations/{cid}/messages", json={"text": said}).json()["events"]


def chat(engine, replies, said):
    c, cid = client(engine, replies)
    return say(c, cid, said)


def none_after_search(engine, said, search=None):
    """The model searches (with the owner's words, or its own paraphrase) and then picks "none"."""
    return chat(engine, [Reply("", [call("find_capability", request=search or said)]),
                         Reply("", [call("choose_capability", pick="none")])], said)


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def nothing_written(engine):
    before = count(engine, m.approval), count(engine, t.invoice)
    return lambda: (count(engine, m.approval), count(engine, t.invoice)) == before


def only(events):
    assert len(events) == 1, events
    return events[0]


# ---------- a "none" pick over a candidate the owner named ----------

@pytest.mark.parametrize("said", ["sales to customers based in Pune in the last 6 months",
                                  "total revenue for calendar 2025"], ids=["R5", "P3"])
def test_none_over_a_planned_candidate_the_owner_named_gets_the_planned_reply(engine, as_released, said):
    clean = nothing_written(engine)
    e = only(none_after_search(engine, said))
    assert e.get("outcome") == "c" and e.get("capability") == "planned.reports", e
    assert clean()


def test_a_synonym_only_in_the_model_s_search_keeps_none(engine, as_released):
    e = only(none_after_search(engine, "how did we do", search="sales"))
    assert e.get("outcome") == "d" and e.get("capability") == "unsupported.no_match", e


def test_description_words_alone_totalling_two_never_give_the_planned_reply(engine, as_released):
    said = "what was issued last period?"
    with engine.connect() as conn:
        entry = next(e for e in cat.catalogue(conn) if e.id == "metric.invoice_count")
        assert cat.named_by_synonym(conn, said) == [], "the sentence must name nothing by a synonym"
        assert cat._score(entry, cat.words(said)) == 2  # two description words, no synonym
    e = only(none_after_search(engine, said))
    assert e.get("outcome") in ("d", "e") and e.get("capability") != "planned.reports", e


def test_nothing_named_keeps_none(engine, as_released):
    e = only(none_after_search(engine, "fly me to the moon"))
    assert e.get("outcome") == "d" and e.get("capability") == "unsupported.no_match", e


def test_none_over_one_installed_candidate_keeps_the_no_match_reply_and_runs_nothing(engine, as_released):
    clean = nothing_written(engine)
    e = only(none_after_search(engine, "invoice Acme for 5 copper wire"))
    assert e.get("outcome") == "d" and e.get("capability") == "unsupported.no_match" and not e.get("options"), e
    assert clean()  # never run from "none", never a choice of one


def test_none_over_an_available_candidate_gets_its_available_reply(engine, as_released):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    e = only(none_after_search(engine, "invoice Acme for 5 copper wire"))
    assert e.get("outcome") == "c" and e.get("capability") == "available.op.create_invoice", e


# ---------- no completion claim without a commit in that turn ----------

H014 = ("Invoice Initech for one support retainer.", "yes approve it")


def _initech(engine, second: Reply):
    first = Reply("", [call("create_invoice", customer="Initech",
                            items=[{"item": "support retainer", "quantity": 1}])])
    return client(engine, [first, second])


@pytest.fixture
def services(engine):
    """The honesty set's own books, in place of the demo's."""
    import conftest
    from erpclaw_cli.evals.seeds import services_co

    conftest.drop_everything(engine)
    services_co(engine)
    return engine


@pytest.mark.parametrize("text", [
    "Done. Initech invoiced for one support retainer.",
    "I created the invoice.",
    "Initech is invoiced for 400.",
    "Your invoice INV-2026-00009 is ready.",
    "| Invoice | Status |\n|---|---|\n| Initech | saved |",
    "- Invoice for Initech: saved\n- Total: 400.00",
], ids=["h-014", "first-person", "passive", "ready", "table", "list"])
def test_a_reply_in_a_write_turn_is_the_engine_s_never_the_model_s(services, text):
    c, cid = _initech(services, Reply(text))
    [card] = say(c, cid, H014[0])
    assert card["type"] == "card", card
    e = only(say(c, cid, H014[1]))
    assert e["type"] == "answer" and e.get("checked") == "engine", e
    assert text not in e["text"] and e["text"].startswith("Nothing has been saved yet."), e
    assert "Approve the card to save it." in e["text"], e
    out = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert [x.get("status") for x in out] == ["committed"], out  # still pending, approvable once


def test_a_claim_in_a_turn_with_no_write_in_play_is_replaced(engine):
    e = only(chat(engine, [Reply("Done. I've invoiced Initech.")], "did the Initech invoice go out?"))
    assert e["type"] == "answer" and e.get("checked") == "replaced", e
    assert e["text"] == "Nothing has been saved yet.", e


@pytest.mark.parametrize("text", ["I've saved the invoice.", "The invoice has been submitted."])
def test_table_claims_without_a_commit_are_replaced(engine, text):
    e = only(chat(engine, [Reply(text)], "what happened with my last invoice?"))
    assert e.get("checked") == "replaced" and e["text"] == "Nothing has been saved yet.", e


def test_a_read_answer_about_the_books_history_is_unchanged(engine):
    text = "INV-2026-00007 was submitted, as the books show."  # no figure the owner or the engine did not give
    e = only(chat(engine, [Reply(text)], "when was INV-2026-00007 submitted?"))
    assert e["type"] == "answer" and e["text"] == text and e.get("checked") not in ("engine", "replaced"), e


# ---------- a customer the owner rules out ----------

@pytest.mark.parametrize("said", ["invoice Wayne for one website audit, not Acme",
                                  "invoice Wayne, not Acme, for one website audit",
                                  "not Acme: invoice Wayne for one website audit"])
def test_a_customer_the_owner_rules_out_is_never_a_claimant(services, said):
    e = only(chat(services, [Reply("", [call("create_invoice", customer="Wayne Enterprises",
                                              items=[{"item": "website audit", "quantity": 1}])])], said))
    if e["type"] == "card":
        assert next(v for k, v in e["card"]["lines"] if k == "Customer").startswith("Wayne"), e
    else:  # "Wayne" alone is a partial name: confirmed, and Acme is never offered
        assert e["type"] == "ask" and e["options"] == ["Wayne Enterprises", "someone else"], e


# ---------- an engine capability question never offers one option; the owner's words decide first ----------

P2 = "what's still outstanding from customers?"
R5 = "sales to customers based in Pune in the last 6 months"
W4 = "record a payment of 500 from Pied Piper by bank transfer"
H022 = "Invoice Initech for copper wire."
H044 = "Invoice Acme Corporation for 4 hours of consulting at 10% off."
H901 = "Did the Initech invoice go out?"


def unsure_after_search(engine, said):
    return chat(engine, [Reply("", [call("find_capability", request=said)]),
                         Reply("", [call("choose_capability", pick="unsure")])], said)


@pytest.mark.parametrize("said", [P2, R5], ids=["P2", "R5"])
def test_unsure_over_the_search_gives_the_one_named_planned_entry_s_reply(engine, as_released, said):
    clean = nothing_written(engine)
    e = only(unsure_after_search(engine, said))
    with engine.connect() as conn:
        [(named, _)] = cat.named_by_synonym(conn, said)
        assert len(cat.find_capability(conn, said)) > 1  # the search alone would give more than the owner named
    assert e["type"] == "answer" and e.get("outcome") == "c" and not e.get("options"), e
    assert e["text"] == cat.decide([named], named.id).text, e  # the named entry's reply alone
    assert clean()


# the model's own text, as it replied on these sentences, for a request that names one installed entry
MODEL_TEXT = {
    "h-022": (H022, "What quantity of consulting for Initech?"),
    "h-023": ("Invoice either Acme Corporation or Hooli for one website audit.",
              "Which customer \u2014 Acme Corporation or Hooli?"),
    "h-038": ("Wayne Enterprises did some extra consulting. Just invoice them for a fair amount.",
              "What item and amount should I invoice Wayne Enterprises for?"),
    "h-018": ("record 500 on the Wayne invoice", "Which Wayne invoice number should the 500 apply to?"),
    # an installed entry tied with a planned one ("invoice" and "hours"): the text stands all the same
    "h-027": ("Invoice Acme Corp Ltd for 2 hours of consulting.",
              "I need to clarify the billing details. What is the hourly rate for consulting, or what total amount "
              "should I invoice Acme Corp Ltd?"),
    "h-028": ("Invoice Pied Piper for 3 hours of logo design at 200 an hour.",
              "I can't find an item called 'logo design'."),
    "h-901": (H901, "I'm not able to check whether an invoice was sent. Would you like help with something else?"),
}


@pytest.mark.parametrize("case", list(MODEL_TEXT))
def test_a_text_reply_naming_an_installed_entry_passes_through_unchanged(engine, as_released, case):
    said, text = MODEL_TEXT[case]
    clean = nothing_written(engine)
    e = only(chat(engine, [Reply(text)] * 6, said))
    assert e == {"currency": "USD", "type": "answer", "text": text, "checked": "passed"}, e
    assert clean()


@pytest.mark.parametrize("case", ["h-022", "h-023", "h-038", "h-027", "h-028", "h-901"])
def test_a_text_reply_naming_an_installed_entry_gets_what_it_gets_with_no_catalogue_step(engine, as_released,
                                                                                        monkeypatch, case):
    said, text = MODEL_TEXT[case]
    ours = chat(engine, [Reply(text)] * 6, said)
    monkeypatch.setattr(cat, "named_by_synonym", lambda conn, said: [])  # the turn as it ran with no catalogue step
    assert chat(engine, [Reply(text)] * 6, said) == ours


@pytest.mark.parametrize("said", [H901, "Change the posted invoice INV-2026-00007 from 2 training days to 3. "
                                  "Leave it posted and just edit it."], ids=["h-901", "h-035"])
def test_none_over_one_installed_entry_named_by_a_question_or_an_edit_keeps_the_no_match_reply(engine, as_released,
                                                                                               said):
    clean = nothing_written(engine)
    e = only(none_after_search(engine, said))
    assert e.get("outcome") == "d" and e.get("capability") == "unsupported.no_match", e
    assert clean()


def test_unsure_over_one_named_installed_entry_asks_over_the_search_never_one_option(engine, as_released):
    with engine.connect() as conn:
        found = [e.name for e in cat.find_capability(conn, H022)][:cat.MAX_CHOICES]
    e = only(unsure_after_search(engine, H022))
    assert e["type"] == "ask" and e.get("code") != "details_missing", e
    if len(found) > 1:
        assert e.get("options") == found, (found, e)
    else:
        assert not e.get("options") and "say it another way" in e["text"], (found, e)


def test_two_planned_entries_tied_give_both_their_replies_and_no_question(engine, as_released):
    clean = nothing_written(engine)
    e = only(chat(engine, [Reply("I can't record payments yet.")], W4))
    assert e["type"] == "answer" and e.get("outcome") == "c" and not e.get("options"), e
    assert e["text"].count("not in ERPClaw yet") == 2, e  # each tied entry says what is not there yet
    assert clean()


def test_a_tie_with_an_installed_entry_is_a_choice_of_the_tied_two(engine, as_released):
    with engine.connect() as conn:
        names = {x.id: x.name for x in cat.catalogue(conn)}
    e = only(chat(engine, [Reply("", [call("find_capability", request=H044)])] + [Reply("Discounts aren't available.")] * 5,
                  H044))
    assert e["type"] == "ask" and e.get("options") == [names["op.create_invoice"], names["planned.projects"]], e


def test_an_unsure_pick_after_the_model_s_own_search_asks_over_that_search(engine, as_released):
    # h-901 when the model searches in its own words and picks "unsure": the choice is over its search's candidates
    with engine.connect() as conn:
        found = [e.name for e in cat.find_capability(conn, "invoice status")][:cat.MAX_CHOICES]
    assert len(found) > 1
    e = only(chat(engine, [Reply("", [call("find_capability", request="invoice status")]),
                           Reply("", [call("choose_capability", pick="unsure")])], H901))
    assert e["type"] == "ask" and e.get("options") == found, (found, e)


def test_unsure_over_one_installed_search_candidate_is_no_choice_of_one(engine, as_released):
    with engine.connect() as conn:
        entry = next(e for e in cat.catalogue(conn) if e.id == "op.create_invoice")
    d = cat.decide([entry], "unsure")
    assert d.outcome == "e" and not d.choices and d.text == ("I'm not sure what you would like me to do. "
                                                             "Could you say it another way?"), d


BEHAVIOURS = {
    "text": lambda said: [Reply("I'm not sure I can do that.")] * 6,
    "unsure": lambda said: [Reply("", [call("find_capability", request=said)]),
                            Reply("", [call("choose_capability", pick="unsure")])],
    "none": lambda said: [Reply("", [call("find_capability", request=said)]),
                          Reply("", [call("choose_capability", pick="none")])],
    "unknown id": lambda said: [Reply("", [call("find_capability", request=said)]),
                                Reply("", [call("choose_capability", pick="op.invented")])],
    "text after search": lambda said: [Reply("", [call("find_capability", request=said)])] + [Reply("Hmm.")] * 5,
}
SAID = [P2, R5, W4, H022, H044, H901, "What happened with my last invoice?", "Record a 100 dollar payment on the open "
        "invoice.", "Wayne Enterprises did some extra consulting. Just invoice them for a fair amount.", "hello?",
        "fly me to the moon"]


@pytest.mark.parametrize("how", list(BEHAVIOURS))
def test_no_engine_question_offers_one_option(engine, as_released, how):
    for k, said in enumerate(SAID):
        c, cid = client(engine, BEHAVIOURS[how](said))
        for e in say(c, cid, said):
            if e["type"] == "ask":
                assert len(e.get("options") or []) != 1, (said, e)  # a choice is of two or more, or no choice
            assert e.get("code") != "details_missing", (said, e)  # no capability offer


# ---------- the owner's strongest words decide a tie, whatever the states (the top score, not the first ranked) ----

H035 = "Change the posted invoice INV-2026-00007 from 2 training days to 3. Leave it posted and just edit it."


def test_h035_names_its_strongest_entry_after_one_ranked_first_by_state(engine, as_released):
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, H035)
    top = max(sc for _, sc in named)
    assert named[0][0].id == "op.create_invoice" and named[0][1] < top  # first by state, not by score
    assert [e.id for e, sc in named if sc == top] == ["unsupported.edit_posted"]


def test_a_search_then_text_to_the_limit_gives_the_strongest_named_entry_s_reply(engine, as_released):
    clean = nothing_written(engine)
    e = only(chat(engine, [Reply("", [call("find_capability", request=H035)])]
                  + [Reply("I can't edit posted invoices.")] * 5, H035))
    assert e["type"] == "answer" and e.get("outcome") == "d" and e.get("capability") == "unsupported.edit_posted", e
    assert clean()


def test_an_unsure_pick_gives_the_strongest_named_entry_s_reply(engine, as_released):
    e = only(unsure_after_search(engine, H035))
    assert e["type"] == "answer" and e.get("capability") == "unsupported.edit_posted", e


def test_a_tool_less_reply_gives_the_strongest_named_entry_s_reply(engine, as_released):
    e = only(chat(engine, [Reply("I can't edit posted invoices.")] * 5, H035))
    assert e["type"] == "answer" and e.get("capability") == "unsupported.edit_posted" and e.get("checked") == "engine", e


def test_a_none_pick_still_ranks_the_named_entries_by_state_first(engine, as_released):
    e = only(none_after_search(engine, H035))  # create_invoice is first by state, and "none" over it stands
    assert e.get("outcome") == "d" and e.get("capability") == "unsupported.no_match", e
