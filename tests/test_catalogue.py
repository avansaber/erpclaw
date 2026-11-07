"""The capability catalogue and request outcomes."""
from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from bookdiff import CONTROL
from erpclaw_api.app import create_app
from erpclaw_assistant import store
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import backup as bk, catalogue as cat
from erpclaw_core.plugins import disable_plugin


def chat(engine, replies, text):
    model = ScriptedModel(replies)
    c = TestClient(create_app(engine, model, embedded_owner=True))  # in-process: the owner
    cid = c.post("/v1/conversations").json()["id"]
    return c.post(f"/v1/conversations/{cid}/messages", json={"text": text}).json()["events"], model


def books(engine):
    with engine.connect() as conn:
        return [bk._dump(conn, t) for t in bk.books_tables(conn) if t.name not in CONTROL]


def find_then(pick, request="x"):
    return [Reply("", [call("find_capability", request=request)]), Reply("", [call("choose_capability", pick=pick)])]


# --- generation -----------------------------------------------------------------------

def test_the_catalogue_is_generated_from_the_registry_and_the_planned_file(engine):
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
    assert entries["op.create_invoice"].state == "installed" and entries["metric.receivables"].state == "installed"
    assert "op.get_metric" not in entries  # metrics are entries; get_metric is how they run
    planned = [e for e in entries.values() if e.state == "planned"]
    assert planned and all(e.kind == "area" and e.id.startswith("planned.") for e in planned)
    assert "planned.sales" not in entries  # delivered by the sales plugin, installed in these books


def test_disabling_a_plugin_moves_its_entries_to_available_and_brings_its_area_back(engine):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    with engine.connect() as conn:
        entries = {e.id: e for e in cat.catalogue(conn)}
    assert entries["op.create_invoice"].state == "available" and entries["metric.sales"].state == "available"
    assert entries["planned.sales"].state == "planned"


# --- search ---------------------------------------------------------------------------

def test_the_search_is_deterministic_bounded_and_finds_by_synonym_and_words(engine):
    with engine.connect() as conn:
        a = [e.id for e in cat.find_capability(conn, "Who OWES us money?")]
        b = [e.id for e in cat.find_capability(conn, "who owes us money")]
        payroll = [e.id for e in cat.find_capability(conn, "run the PAYROLL for this month")]
        accents = [e.id for e in cat.find_capability(conn, "Révenue this year")]
        many = cat.find_capability(conn, "invoice sales payment stock payroll bank project budget crm")
        revenue = [e.id for e in cat.find_capability(conn, "revenue this year")]
    assert a == b and a[0] == "metric.receivables"
    assert payroll[0] == "planned.payroll"
    assert "metric.sales" in revenue and accents == revenue
    assert len(many) <= cat.MAX_CANDIDATES
    states = [e.state for e in many]
    assert states == sorted(states, key=lambda s: cat.STATE_RANK[s])  # installed before planned, whatever the score


# --- outcomes ---------------------------------------------------------------------------

def test_outcome_a_runs_only_the_chosen_metric_with_its_own_period(engine):
    replies = find_then("metric.sales", "sales last month") + [Reply("", [call("get_metric", metric="sales",
                                                                                  period="last month")])]
    ev, model = chat(engine, replies, "what were sales last month?")
    assert ev[-1]["type"] == "answer" and "Sales" in ev[-1]["text"] and "Sep" in ev[-1]["text"]  # pinned today: 26 Oct
    assert model.tools_seen[2] == ["get_metric"]  # exactly the chosen capability's own tool
    assert store.demand() == []


def test_outcome_a_for_a_write_proposes_the_card_a_direct_request_would(engine):
    replies = find_then("op.create_invoice", "bill acme") + [Reply("", [call("create_invoice", customer="Acme Corporation",
                                                                               items=[{"item": "copper wire", "quantity": 5}])])]
    ev, model = chat(engine, replies, "bill Acme Corporation for 5 copper wire")
    assert [e["type"] for e in ev] == ["card"] and ["Total", "140.00 USD"] in ev[0]["card"]["lines"]
    assert model.tools_seen[2] == ["create_invoice"]


def test_a_call_to_another_tool_after_choosing_is_refused_as_unsure(engine):
    before = books(engine)
    replies = find_then("metric.sales", "sales") + [Reply("", [call("get_metric", metric="receivables")])]
    ev, _ = chat(engine, replies, "sales this year")
    assert ev[-1]["type"] == "ask" and ev[-1]["outcome"] == "e"
    replies = find_then("metric.sales", "sales") + [Reply("", [call("create_invoice", customer="Acme Corporation",
                                                                    items=[{"item": "copper wire", "quantity": 1}])])]
    ev, _ = chat(engine, replies, "sales this year")
    assert ev[-1]["outcome"] == "e"
    assert books(engine) == before


@pytest.mark.parametrize("pick, said, outcome", [("planned.payroll", "run payroll", "c"), ("none", "fly me to the moon", "d"),
                                                    ("unsure", "invoice", "e"), ("op.invented", "invoice", "e")])
def test_outcomes_c_d_e_answer_from_fixed_text_record_demand_and_change_no_books(engine, pick, said, outcome):
    before = books(engine)
    ev, _ = chat(engine, find_then(pick, said), said)
    assert ev[-1]["outcome"] == outcome
    text = ev[-1]["text"]
    if outcome == "c":
        assert "planned for release v3.1" in text
    nums = {w for w in text.replace("(", " ").replace(")", " ").split() if any(ch.isdigit() for ch in w)}
    assert nums <= {"v3.1.", "v3.1", "1", "2", "3"}, nums  # no figure: only a release name or choice numbers
    rows = store.demand()
    assert len(rows) == (1 if outcome in ("c", "d") else 0)
    if rows:
        assert rows[0][2] == outcome and rows[0][4] == said
    assert books(engine) == before  # demand lives outside the books


def test_the_assistant_store_is_private_and_not_the_books(engine):
    chat(engine, find_then("none", "fly"), "fly me to the moon")
    p = store.path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_a_planned_entry_that_becomes_installed_changes_its_outcome_with_no_prompt_change(engine, tmp_path, monkeypatch):
    from erpclaw_core.contracts import Contract, Input, Result
    from erpclaw_core.plugins import REGISTRY, install_record
    from test_plugins import plug

    for name in ("create_sales_order", "deliver_order", "create_credit_note"):  # switched off, as released: an
        monkeypatch.setattr(REGISTRY.contracts[name], "planned", "Core")  # installed op sharing "order" ranks first

    planned = tmp_path / "planned.json"
    planned.write_text(json.dumps([{"id": "planned.tea", "kind": "area", "name": "tea rounds", "wave": "v3.1",
                                    "synonyms": ["tea", "tea round"], "installed_as": ["extra"]}]))
    monkeypatch.setattr(cat, "PLANNED", planned)

    class TeaIn(Input):
        pass

    tea = Contract("order_tea", "extra", "read", "Order the tea round.", TeaIn, run=lambda ctx, inp: Result("Tea is on."),
                   synonyms=("tea", "tea round"))
    with engine.connect() as conn:
        first = cat.find_capability(conn, "order a tea round")
    assert first[0].id == "planned.tea"
    extra = plug("extra", contracts=[tea])
    monkeypatch.setitem(REGISTRY.plugins, "extra", extra)
    monkeypatch.setitem(REGISTRY.contracts, "order_tea", tea)
    monkeypatch.setitem(REGISTRY.contract_plugin, "order_tea", "extra")
    with engine.begin() as conn:
        install_record(conn, extra)
    with engine.connect() as conn:
        after = cat.find_capability(conn, "order a tea round")
    assert after[0].id == "op.order_tea" and "planned.tea" not in [e.id for e in after]


# --- the phases are enforced ------------------------------------------

def test_free_text_after_a_search_is_never_the_answer(engine):
    """Model text must not bypass the choice and invent a status or a figure."""
    before = books(engine)
    replies = [Reply("", [call("find_capability", request="payroll")]),
               Reply("Payroll is fully supported and costs 1,234.00 USD."),
               Reply("Still text: payroll is installed.")]
    ev, model = chat(engine, replies, "run payroll")
    # the owner's own words name payroll: its planned reply, the engine's text, never the model's
    assert [e["type"] for e in ev] == ["answer"] and ev[0]["outcome"] == "c" and ev[0]["capability"] == "planned.payroll"
    assert "1,234" not in ev[0]["text"] and "installed" not in ev[0]["text"]
    assert model.tools_seen[1] == ["choose_capability"]  # after a search, only the choice is offered
    assert "Do not answer in text" in model.seen[2][-1]["content"]  # one reprompt first
    assert store.demand() == [] and books(engine) == before


def test_free_text_after_choosing_is_never_the_answer(engine):
    replies = find_then("metric.sales", "sales") + [Reply("Sales were 9,999.00 USD."), Reply("Again 9,999.00 USD.")]
    ev, _ = chat(engine, replies, "sales this year")
    assert ev[-1]["outcome"] == "e" and "9,999" not in ev[-1]["text"]


@pytest.mark.parametrize("second", [call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 1}]),
                                    call("find_capability", request="again")], ids=["direct tool", "second search"])
def test_after_a_search_only_the_choice_runs(engine, second):
    before = books(engine)
    replies = [Reply("", [call("find_capability", request="sales")]), Reply("", [second])]
    ev, _ = chat(engine, replies, "sales")
    assert [e["type"] for e in ev] == ["ask"] and ev[0]["outcome"] == "e"
    assert books(engine) == before


def test_two_choices_in_one_reply_run_nothing(engine):
    """In the catalogue path a reply is one call or nothing."""
    before = books(engine)
    replies = [Reply("", [call("find_capability", request="payroll")]),
               Reply("", [call("choose_capability", pick="none"), call("choose_capability", pick="planned.payroll")])]
    ev, _ = chat(engine, replies, "run payroll")
    assert [e["type"] for e in ev] == ["answer"] and ev[0]["capability"] == "planned.payroll"  # the owner's words
    assert store.demand() == [] and books(engine) == before


def test_a_choice_with_nothing_searched_is_unsure(engine):
    ev, _ = chat(engine, [Reply("", [call("choose_capability", pick="metric.sales")])], "sales")
    assert ev[-1]["outcome"] == "e"


@pytest.mark.parametrize("order", ["direct first", "catalogue first"])
@pytest.mark.parametrize("direct", [call("get_metric", metric="receivables"),
                                    call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 1}])],
                         ids=["read", "write"])
def test_a_reply_mixing_the_catalogue_with_other_tools_runs_nothing(engine, order, direct):
    """In either order, nothing in a mixed reply runs: no figure, no card, no demand."""
    before = books(engine)
    catalogue_call = call("find_capability", request="payroll")
    calls = [direct, catalogue_call] if order == "direct first" else [catalogue_call, direct]
    ev, _ = chat(engine, [Reply("", calls)], "run payroll and also something else")
    # nothing searched, so no candidates: the owner's own words decide, and payroll is planned
    assert [e["type"] for e in ev] == ["answer"] and ev[0]["capability"] == "planned.payroll"
    assert store.demand() == [] and books(engine) == before


def test_a_mixed_reply_naming_nothing_asks_and_claims_nothing(engine):
    """With nothing searched and nothing named by the owner, the reply asks and claims nothing."""
    before = books(engine)
    calls = [call("find_capability", request="x"), call("get_metric", metric="receivables")]
    ev, _ = chat(engine, [Reply("", calls)], "do the thing and also something else")
    assert ev[0]["text"] == "I'm not sure what you would like me to do. Could you say it another way?"
    assert store.demand() == [] and books(engine) == before


@pytest.mark.parametrize("calls", [
    [call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 1}]), call("get_metric", metric="sales")],
    [call("get_metric", metric="sales"), call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 1}])],
    [call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 1}]),
     call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 2}])],
], ids=["allowed then wrong", "wrong then allowed", "the chosen write twice"])
def test_after_choosing_a_reply_of_more_than_one_call_runs_nothing(engine, calls):
    """No card can be made before a later call in the same reply is refused, and no second card."""
    from erpclaw_core import models as m
    from sqlalchemy import func, select

    with engine.connect() as conn:
        cards = conn.execute(select(func.count()).select_from(m.approval)).scalar_one()
    ev, _ = chat(engine, find_then("op.create_invoice", "invoice acme") + [Reply("", calls)], "invoice Acme")
    assert [e["type"] for e in ev] == ["ask"] and ev[0]["outcome"] == "e"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.approval)).scalar_one() == cards  # no card


# --- an entry the owner's words name by a synonym is never pushed out by description-word matches ------------------

def _desc_only(i: int) -> cat.Entry:
    return cat.Entry(f"op.write_{i}", "operation", f"write {i}", "change a posted invoice by its days", (), "installed",
                     plugin="sales", target=f"write_{i}")


EDIT_POSTED = cat.Entry("unsupported.edit_posted", "operation", "edit a posted invoice", "edit a posted invoice",
                        ("change the posted invoice", "edit a posted invoice"), "unsupported",
                        reply="A posted invoice is never edited.")


def test_a_synonym_named_entry_ranks_above_five_installed_entries_matched_only_by_description(monkeypatch):
    """Five installed writes share four description words with the request (change, posted, invoice, days); the
    unsupported entry is named by a whole synonym phrase. It must be among the five candidates, first."""
    monkeypatch.setattr(cat, "catalogue", lambda conn: sorted([_desc_only(i) for i in range(5)] + [EDIT_POSTED],
                                                              key=lambda e: e.id))
    found = cat.find_capability(None, "Change the posted invoice INV-2026-00007 from 2 days to 3")
    assert len(found) == cat.MAX_CANDIDATES and found[0].id == "unsupported.edit_posted"


def test_from_is_a_stop_word():
    assert cat.words("Change it from 2 days to 3") == ["change", "2", "days", "3"]
