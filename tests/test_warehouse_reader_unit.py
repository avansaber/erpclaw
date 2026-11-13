"""Warehouse reader units: in-memory records only; no engine, graph, database, or model invocation."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from erpclaw_assistant import resolve as rv
from erpclaw_assistant.resolve_warehouse import _company_warehouse_names as read_company_warehouse_names
from erpclaw_core.common import Ctx, Refusal
import erpclaw_sales as sales
from erpclaw_sales import stock_sales as stock
from erpclaw_assistant import resolve_args, resolve_inventory, resolve_policy, resolve_records, resolve_types, resolve_warehouse


ITEMS = [
    {"id": "copper", "name": "Copper Wire", "unit": "unit", "standard_rate": "2.5", "kind": "stock"},
    {"id": "board", "name": "Circuit Board", "unit": "unit", "standard_rate": "20", "kind": "stock"},
]
CUSTOMERS = [{"id": "acme", "name": "Acme Corporation", "email": None, "city": None}]
WAREHOUSES = [
    {"warehouse": "main", "name": "Main Store", "qty": "20"},
    {"warehouse": "raw", "name": "Raw Materials Store", "qty": "10"},
    {"warehouse": "day", "name": "Monday 2 Store", "qty": "8"},
]
CONTRACT = SimpleNamespace(name="create_invoice", input_model=sales.CreateInvoice)
SCOPE = "create_invoice#0:"


class NoBooks:
    def execute(self, *args, **kwargs):
        raise AssertionError("A warehouse reader unit must not access a database")


@pytest.fixture
def memory(monkeypatch):
    _replacement = (lambda ctx: CUSTOMERS)
    for _module in (rv, resolve_args, resolve_inventory, resolve_records):
        monkeypatch.setattr(_module, 'customers', _replacement)
    _replacement = (lambda ctx: ITEMS)
    for _module in (rv, resolve_args, resolve_inventory, resolve_policy, resolve_types):
        monkeypatch.setattr(_module, '_items', _replacement)
    monkeypatch.setattr(stock, "holdings", lambda ctx, item: WAREHOUSES)
    _replacement = (lambda ctx: {r['name'].lower() for r in WAREHOUSES})
    for _module in (rv, resolve_warehouse):
        monkeypatch.setattr(_module, '_company_warehouse_names', _replacement)
    _replacement = (lambda *a, **kw: {'warehouses': WAREHOUSES})
    for _module in (rv, resolve_args, resolve_policy, resolve_warehouse):
        monkeypatch.setattr(_module, 'call_service', _replacement)
    assert read_company_warehouse_names(None) == {r['name'].lower() for r in WAREHOUSES}
    return Ctx(NoBooks(), "company", "Test Company", "USD", date(2026, 11, 4))


def request(text, memory, *, lines=None, session=None, scope=SCOPE):
    args = {"customer": "Acme Corporation", "items": lines or [{"item": "Copper Wire", "quantity": 5}]}
    session = session or rv.Session()
    result = rv.resolve_args(memory, CONTRACT, args, text, session, scope=scope)
    return args, result, session


def account(text, memory, result, session, scope=SCOPE):
    rv.account(memory, text, [(CONTRACT, result.args)], [result], 0, session, [scope])


def test_owner_named_warehouse_is_read_and_accounted_without_model_argument(memory):
    text = "invoice Acme Corporation for 5 Copper Wire from Raw Materials Store"
    _, result, session = request(text, memory)
    assert result.args["items"] == [{"item": "Copper Wire", "quantity": "5", "warehouse": "Raw Materials Store"}]
    account(text, memory, result, session)


def test_model_only_warehouse_is_removed_and_no_default_is_stored(memory):
    text = "invoice Acme Corporation for 5 Copper Wire"
    _, result, session = request(text, memory, lines=[{"item": "Copper Wire", "quantity": 5,
                                                      "warehouse": "Raw Materials Store"}])
    assert "warehouse" not in result.args["items"][0]
    account(text, memory, result, session)


def test_unknown_warehouse_asks_exact_line_with_explicit_refs_then_typed_name_binds(memory):
    text = "invoice Acme Corporation for 5 Copper Wire from the Pune warehouse"
    session = rv.Session()
    with pytest.raises(Refusal) as error:
        request(text, memory, session=session)
    assert error.value.code == "warehouse_unknown"
    assert session.pending["field"] == SCOPE + "items.0.warehouse"
    assert session.pending["choices"][:2] == [
        {"code": "q1-1", "label": "Main Store (20 on hand)", "ref": "Main Store"},
        {"code": "q1-2", "label": "Raw Materials Store (10 on hand)", "ref": "Raw Materials Store"},
    ]
    assert rv.match_answer(session.pending, "Raw Materials Store") == ("choice", "q1-2")
    assert session.answer("q1-2")
    _, result, _ = request(text, memory, session=session)
    assert result.args["items"][0]["warehouse"] == "Raw Materials Store"
    account(text, memory, result, session)


def test_warehouse_reference_matching_is_limited_to_warehouse_fields():
    choices = [{"code": "q1-1", "label": "Name (20 on hand)", "ref": "Name"}]
    assert rv.match_answer({"id": "q1", "field": "items.0.warehouse", "choices": choices}, "Name") == ("choice", "q1-1")
    assert rv.match_answer({"id": "q1", "field": "customer", "choices": choices}, "Name") is None


def test_two_item_sources_stay_with_their_items_when_model_lines_are_reversed(memory):
    text = ("invoice Acme Corporation for 5 Copper Wire from Main Store and "
            "3 Circuit Board from Raw Materials Store")
    _, result, session = request(text, memory, lines=[
        {"item": "Circuit Board", "quantity": 3}, {"item": "Copper Wire", "quantity": 5}])
    assert [(x["item"], x["quantity"], x["warehouse"]) for x in result.args["items"]] == [
        ("Circuit Board", "3", "Raw Materials Store"), ("Copper Wire", "5", "Main Store")]
    account(text, memory, result, session)


def test_warehouse_answer_from_another_write_never_binds(memory):
    session = rv.Session(bindings={"create_invoice#1:items.0.warehouse": "Main Store"})
    with pytest.raises(Refusal) as error:
        request("invoice Acme Corporation for 5 Copper Wire from Pune warehouse", memory, session=session)
    assert error.value.code == "warehouse_unknown"
    assert error.value.data["field"] == SCOPE + "items.0.warehouse"


def test_calendar_and_number_words_in_warehouse_do_not_become_other_values(memory):
    text = "invoice Acme Corporation for 5 Copper Wire from Monday 2 Store"
    _, result, session = request(text, memory)
    assert result.args["items"][0]["warehouse"] == "Monday 2 Store"
    assert result.not_done == []
    occurrences = rv.inventory(memory, text, ITEMS)
    assert [(o.kind, o.said) for o in occurrences if o.kind in {"date", "warehouse"}] == [
        ("warehouse", "Monday 2 Store")]
    assert [o.value for o in occurrences if o.kind == "figure"] == [Decimal(5)]
    account(text, memory, result, session)


def test_from_inside_stored_item_name_is_not_a_warehouse():
    assert rv.warehouse_words("invoice Acme for 5 Wire from Pune", [{"name": "Wire from Pune"}]) == []


def test_source_does_not_attach_across_sentence_and_unknown_phrase_is_inventoried(memory):
    text = "invoice Acme Corporation for 5 Copper Wire. From Pune warehouse."
    phrases = rv.warehouse_words(text, ITEMS, CUSTOMERS)
    assert len(phrases) == 1 and phrases[0].item is None
    occurrences = rv.inventory(memory, text, ITEMS)
    assert any(o.kind == "warehouse" and o.said == "Pune warehouse" for o in occurrences)


def test_a_service_item_cannot_silently_consume_a_warehouse(memory, monkeypatch):
    _replacement = (lambda ctx: [{**ITEMS[0], 'kind': 'service'}])
    for _module in (rv, resolve_args, resolve_inventory, resolve_policy, resolve_types):
        monkeypatch.setattr(_module, '_items', _replacement)
    _replacement = (lambda ctx: {'main store'})
    for _module in (rv, resolve_warehouse):
        monkeypatch.setattr(_module, '_company_warehouse_names', _replacement)  # these books name that warehouse
    with pytest.raises(Refusal) as error:
        request("invoice Acme Corporation for 5 Copper Wire from Main Store", memory)
    assert error.value.code == "warehouse_ambiguous"


def test_i2_refuses_a_warehouse_dropped_by_an_unmarked_line_contract(memory):
    text = "invoice Acme Corporation for 5 Copper Wire from Raw Materials Store"
    offered = SimpleNamespace(name="create_invoice", input_model=sales.OfferedInvoice)
    args = {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": "5"}]}
    result = rv.Result(args)
    with pytest.raises(Refusal) as error:
        rv.account(memory, text, [(offered, args)], [result], 0, rv.Session(), [SCOPE])
    assert error.value.code == "warehouse_ambiguous"


def test_i2_cannot_claim_another_lines_warehouse(memory):
    text = ("invoice Acme Corporation for 5 Copper Wire from Main Store and "
            "3 Circuit Board from Raw Materials Store")
    args = {"customer": "Acme Corporation", "items": [
        {"item": "Copper Wire", "quantity": "5", "warehouse": "Raw Materials Store"},
        {"item": "Circuit Board", "quantity": "3", "warehouse": "Main Store"}]}
    with pytest.raises(Refusal) as error:
        account(text, memory, rv.Result(args), rv.Session())
    assert error.value.code == "warehouse_ambiguous"


def test_invoice_default_source_question_is_bound_to_the_correct_line():
    refusal = Refusal("Which warehouse?", "warehouse_ambiguous", options=["Main Store (20 on hand)"],
                      choices=[{"label": "Main Store (20 on hand)", "ref": "Main Store"}])
    def read():
        raise refusal
    with pytest.raises(Refusal) as error:
        sales._invoice_warehouse(1, read)
    session = rv.Session()
    rv.with_codes(error.value, session, SCOPE)
    assert session.pending["field"] == SCOPE + "items.1.warehouse"
    assert session.pending["choices"][0]["ref"] == "Main Store"


def test_optional_m4_line_models_and_model_hidden_warehouse():
    assert rv._line_model(sales.DeliverOrder, "items") is sales.DeliveryItem
    assert rv._line_model(sales.CreateCreditNote, "items") is sales.CreditItem
    schema = sales._offered_invoice({})
    assert "warehouse" not in schema["properties"]["items"]["items"]["properties"]
    assert "order" not in schema["properties"]


def test_from_inside_existing_memo_never_changes_stock_source(memory):
    text = "invoice Acme Corporation for 5 Copper Wire memo from Raw Materials Store"
    _, result, session = request(text, memory)
    assert "warehouse" not in result.args["items"][0]
    assert [row["part"] for row in result.not_done] == ["memo"]
    account(text, memory, result, session)
