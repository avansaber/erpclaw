"""A question lists its control actions, so a client can show them: each `ask` carries, after its candidates, the
control actions the engine accepts at that question, coded "<question id>-<name>", marked control, and labelled from
the language table; `options` stays the candidates' labels only. A question naming its own "someone else" (a confirm)
is not given a second one; a free question has nothing to choose, so only cancel; a capability question has none.
Every control choice an ask lists is one the engine's own answer matching accepts for that same question.
"""
from __future__ import annotations

import copy

import pytest

from erpclaw_assistant import graph, resolve
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_party


def inv(customer, quantity=5):
    return call("create_invoice", customer=customer, items=[{"item": "copper wire", "quantity": quantity}])


def last_ask(events):
    asks = [e for e in events if e["type"] == "ask"]
    assert asks, events
    return asks[-1]


def controls(ask):
    return [c for c in ask["choices"] if c["control"]]


def _traders(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "customer", "A&B Traders", "ab@traders.example", "Leeds")
        create_party(ctx, "customer", "AB Traders", "info@abtraders.example", "York")


def field_ask(engine):
    _traders(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [inv("Traders")])] * 3))
    return a, last_ask(a.turn("c1", "Invoice Traders for 5 copper wire"))


def confirm_ask(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [inv("Wayne")])] * 3))
    return a, last_ask(a.turn("c1", "Invoice Wayne for 5 copper wire"))


def free_ask(engine):
    pay = Reply("", [call("record_payment", customer="Initech", method="check")])
    a = Assistant(engine, ScriptedModel([pay] * 3))
    return a, last_ask(a.turn("c1", "Initech paid by check"))


def approval_ask(engine):
    """A payment card, then a second open invoice for its customer: approving it asks which invoice, and the next
    message shows that question with its codes."""
    from erpclaw_core.plugins import REGISTRY

    pay = Reply("", [call("record_payment", customer="Initech", amount="100", method="check")])
    a = Assistant(engine, ScriptedModel([pay] * 3))
    card = next(e for e in a.turn("c1", "Initech paid 100 by check") if e["type"] == "card")
    extra = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                              {"customer": "Initech", "items": [{"item": "copper wire", "quantity": 5}]})
    assert approvals.approve(engine, extra.approval_id, extra.digest).status == "committed"
    assert approvals.approve(engine, card["approval_id"], card["digest"]).status == "superseded"
    return a, last_ask(a.turn("c1", "hmm"))


ASKS = {"field": (field_ask, ["someone_else", "cancel"]), "confirm": (confirm_ask, ["cancel"]),
        "free": (free_ask, ["cancel"]), "approval": (approval_ask, ["someone_else", "cancel"])}


@pytest.mark.parametrize("which", sorted(ASKS))
def test_each_question_lists_its_control_actions_after_its_candidates_and_options_stay_the_candidates(engine, which):
    make, names = ASKS[which]
    _, ask = make(engine)
    qid = ask["question_id"]
    assert qid, ask
    assert [c["code"] for c in controls(ask)] == [f"{qid}-{n}" for n in names], ask
    assert [c["label"] for c in controls(ask)] == [resolve.table()["text"]["controls"][n] for n in names]
    ranks = [c["control"] for c in ask["choices"]]
    assert ranks == sorted(ranks), ask  # candidates first, control actions last
    assert ask["options"] == [c["label"] for c in ask["choices"] if not c["control"]], ask


@pytest.mark.parametrize("which, name", [(w, n) for w in sorted(ASKS) for n in ASKS[w][1]])
def test_every_control_choice_listed_is_one_the_engine_accepts_for_that_question(engine, monkeypatch, which, name):
    """The round trip: each listed control code, sent back as the owner's choice at that question, is matched by the
    engine's own answer matching as that control action, before the model runs."""
    a, ask = ASKS[which][0](engine)
    listed = {c["code"]: c["label"] for c in controls(ask)}
    code = f"{ask['question_id']}-{name}"
    assert code in listed, ask
    matched, real = [], resolve.match_answer

    def recording(*args, **kw):
        matched.append(real(*args, **kw))
        return matched[-1]

    monkeypatch.setattr(resolve, "match_answer", recording)
    a.turn("c1", listed[code], choice=code)
    assert matched[:1] == [("control", name)], (which, name, matched)


def test_a_capability_question_lists_no_control_actions():
    ask = graph._asked({"type": "ask", "text": "Which of these did you mean?", "code": "capability_unsure",
                        "options": ["Payroll", "Timesheets"], "outcome": "e",
                        "data": {"question_id": "q2", "choices": [{"code": "q2-1", "label": "Payroll"},
                                                                  {"code": "q2-2", "label": "Timesheets"}]}})
    assert ask["kind"] == "capability" and controls(ask) == [] and ask["options"] == ["Payroll", "Timesheets"]


def test_a_question_without_an_id_lists_no_control_actions():
    """A control code is "<question id>-<name>": with no id there is nothing the engine could match."""
    ask = graph._asked({"type": "ask", "text": "Which?", "code": "customer_ambiguous", "options": ["A", "B"],
                        "data": {"choices": [{"code": "x-1", "label": "A"}, {"code": "x-2", "label": "B"}]}})
    assert controls(ask) == []


def test_the_control_labels_come_from_the_language_table(monkeypatch):
    """A stand-in second table (only English is supported for now): its labels are the ones
    listed, never literals in the code."""
    other = copy.deepcopy(resolve.TABLES["en"])
    other["text"]["controls"] = {"someone_else": "Another one", "cancel": "Stop here"}
    monkeypatch.setitem(resolve.TABLES, "zz", other)
    monkeypatch.setattr(resolve, "LANGUAGE", "zz")
    ask = graph._asked({"type": "ask", "text": "Which?", "code": "customer_ambiguous", "options": ["A", "B"],
                        "data": {"question_id": "q5", "choices": [{"code": "q5-1", "label": "A"},
                                                                  {"code": "q5-2", "label": "B"}]}})
    assert [(c["code"], c["label"]) for c in controls(ask)] == [("q5-someone_else", "Another one"),
                                                               ("q5-cancel", "Stop here")]
    assert ask["options"] == ["A", "B"]


def test_the_model_never_sees_the_control_choices(engine):
    """What the engine tells the model is the question's text and card, never its client-side choices."""
    sent = []

    class Recording(ScriptedModel):
        def complete(self, messages, tools):
            sent.append(messages)
            return super().complete(messages, tools)

    _traders(engine)
    a = Assistant(engine, Recording([Reply("", [inv("Traders")]), Reply("Which one?", [])]))
    ask = last_ask(a.turn("c1", "Invoice Traders for 5 copper wire"))
    a.turn("c1", "hmm")
    later = " ".join(str(m.get("content")) for m in sent[-1])
    assert all(c["code"] not in later for c in controls(ask)), later
