"""First stock item questions and the owner's answers."""
import pytest

from erpclaw_assistant.model import Reply
from erpclaw_assistant.resolve_state import Session, match_answer
from erpclaw_core.common import company_ctx
from erpclaw_stock import service as stock
from test_own_books import SETUP, approved, client, last_ask, only_card, own_books, pick, propose, say, tool


def test_non_latin_warehouse_references():
    session = Session()
    session.ask("add_item#0:warehouse", "Choose", "warehouse_unknown", ["Create 倉庫"], ["倉庫"])
    for reply in ("गोदाम", "東京", ""):
        assert match_answer(session.pending, reply) is None
    assert match_answer(session.pending, "倉庫") == ("choice", "q1-1")


@pytest.mark.parametrize("offered,unoffered", [
    ("東京倉庫1", "大阪倉庫1"),
    ("दिल्ली1", "मुंबई1"),
])
def test_mixed_script_warehouse_references(offered, unoffered):
    session = Session()
    session.ask("add_item#0:warehouse", "Choose", "warehouse_unknown", [f"Create {offered}"], [offered])
    assert match_answer(session.pending, unoffered) is None
    assert match_answer(session.pending, offered) == ("choice", "q1-1")
    assert match_answer(session.pending, f"Create {unoffered}") is None


def test_latin_warehouse_reference_and_position():
    session = Session()
    session.ask("add_item#0:warehouse", "Choose", "warehouse_unknown", ["Create Main Store"], ["Main Store"])
    for reply in ("Main Store", "main store", "1"):
        assert match_answer(session.pending, reply) == ("choice", "q1-1")


def test_ambiguous_account_reference_does_not_choose_position():
    session = Session()
    session.ask("add_item#0:inventory_account", "Choose", "account_ambiguous",
                ["First account", "Second account"], ["1-", "1."])
    assert match_answer(session.pending, "1") is None


def item_words(words):
    args = {"name": "Copper Coil", "kind": "stock", "valuation_method": "fifo"}
    if "meter" in words:
        args["unit"] = "meter"
    if "12.00" in words:
        args["price"] = "12.00"
    return Reply("", [tool("add_item", **args)])


@pytest.fixture
def ready(engine):
    own_books(engine)
    approved(engine, propose(engine, "set_up_company", SETUP))
    return engine


@pytest.mark.parametrize("initial,answer,code,line", [
    ("Add stock item Copper Coil using FIFO at 12.00", "meter", "item_unit_missing", ["Unit", "meter"]),
    ("Add stock item Copper Coil using FIFO sold by meter", "12.00", "amount_missing", ["Selling price", "12.00 USD per meter"]),
])
def test_missing_detail_through_chat(ready, initial, answer, code, line):
    c, cid, _ = client(ready, item_words)
    ask = last_ask(say(c, cid, initial))
    if ask["code"] == "item_kind_missing":
        ask = last_ask(say(c, cid, "", pick(ask, "Goods")))
    if ask["code"] == "valuation_method_missing":
        ask = last_ask(say(c, cid, "", pick(ask, "FIFO")))
    assert ask["code"] == code
    events = say(c, cid, answer)
    for code, label in (("item_kind_missing", "Goods"), ("valuation_method_missing", "FIFO")):
        asks = [event for event in events if event["type"] == "ask"]
        if asks and asks[-1]["code"] == code:
            events = say(c, cid, "", pick(asks[-1], label))
    card = only_card(events)
    assert line in card["card"]["lines"]


def test_warehouse_choice_through_chat(ready):
    with ready.begin() as conn:
        ctx = company_ctx(conn)
        stock.create_warehouse(ctx, "Main Warehouse", default=True)
        stock.create_warehouse(ctx, "West Store")
    c, cid, _ = client(ready, item_words)
    ask = last_ask(say(c, cid, "Add stock item Copper Coil using FIFO sold by meter at 12.00"))
    if ask["code"] == "item_kind_missing":
        ask = last_ask(say(c, cid, "", pick(ask, "Goods")))
    if ask["code"] == "valuation_method_missing":
        ask = last_ask(say(c, cid, "", pick(ask, "FIFO")))
    assert ask["code"] == "warehouse_ambiguous"
    card = only_card(say(c, cid, "", pick(ask, "West Store")))
    assert ["Warehouse", "West Store"] in card["card"]["lines"]
