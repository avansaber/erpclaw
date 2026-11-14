"""Name confirmation stays in front of the stocked-item card."""
import pytest
from sqlalchemy import select

from erpclaw_assistant.resolve_state import Session, match_answer
from erpclaw_core import models as m
from test_stock_first_item import ARGS, approved, preview, propose, ready, refusal


def test_duplicate_and_near_names_are_decided_before_stock_card(ready):
    approved(ready, propose(ready, "add_item", ARGS | {
        "name": "Copper Wire", "kind": "service", "valuation_method": None}))
    r = refusal(lambda: preview(ready, **(ARGS | {"name": "COPPER  WIRE."})))
    assert (r.code, r.message) == ("item_exists", "Copper Wire is already an item.")
    near = ARGS | {"name": "Copper Cable"}
    r = refusal(lambda: preview(ready, **near))
    assert (r.code, r.data["field"], r.message) == ("item_near_ambiguous", "new_item",
        "Did you mean Copper Wire, already an item, or is Copper Cable a new one?")
    assert [c["ref"] for c in r.data["choices"]] == [False, True]
    r = refusal(lambda: preview(ready, **(near | {"new_item": False})))
    assert (r.code, r.message) == ("item_exists", "Copper Wire is already an item.")
    card = propose(ready, "add_item", near | {"new_item": True})
    assert "Copper Wire is already an item; this adds Copper Cable as another one." in card.card["warnings"]
    approved(ready, card)
    with ready.connect() as conn:
        assert set(conn.execute(select(m.item.c.name, m.item.c.kind)).all()) == {
            ("Copper Wire", "service"), ("Copper Cable", "stock")}


@pytest.fixture(params=["warehouse", "inventory_account"])
def stock_question(request):
    def question(refs, labels=None):
        session = Session()
        session.ask(f"add_item#0:{request.param}", "Choose", "warehouse_unknown",
                    labels or [f"Create {ref}" for ref in refs], refs)
        return session.pending
    return question


@pytest.mark.parametrize("offered,reply", [
    ("Store ١", "Store ٢"), ("Store ٢", "Store ١"),
    ("Store １", "Store ２"), ("Store ¹", "Store ²"),
    ("Café", "Cafe"), ("राम", "रम"), ("Store +", "Store"),
])
@pytest.mark.parametrize("single", [True, False])
def test_unoffered_written_name_never_selects(stock_question, offered, reply, single):
    """Removing the written-form guard must expose a dropped mark or symbol collision."""
    refs = [offered] if single else [offered, "West Store"]
    assert match_answer(stock_question(refs), reply) is None
    assert match_answer(stock_question(refs), offered) == ("choice", "q1-1")


def test_name_case_spacing_and_punctuation_and_choice_number(stock_question):
    """Removing normal comparison or position matching breaks ordinary owner answers."""
    pending = stock_question(["Main Store", "Store 2"])
    assert match_answer(pending, "  MAIN   STORE!  ") == ("choice", "q1-1")
    assert match_answer(pending, "2") == ("choice", "q1-2")
    assert match_answer(pending, "Store 2") == ("choice", "q1-2")


def test_punctuation_only_is_not_a_label_or_reference(stock_question):
    """Removing the empty-normalized guard selects a punctuation-only label or reference."""
    assert match_answer(stock_question(["West Store"], ["---"]), "---") is None
    assert match_answer(stock_question(["---"], ["Create store"]), "---") is None


def test_ambiguous_normalized_references_stay_open(stock_question):
    """Returning the first normalized reference guesses between offered choices."""
    pending = stock_question(["Main-Store", "Main.Store"])
    assert match_answer(pending, "MainStore") is None


def test_numbered_label_does_not_drop_significant_characters(stock_question):
    """Removing the written-form guard from numbered labels selects a different name."""
    pending = stock_question(["राम"], ["Create राम"])
    assert match_answer(pending, "1. Create रम") is None
