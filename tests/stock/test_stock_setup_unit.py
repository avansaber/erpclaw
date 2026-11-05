"""Pure stock/2 setup contracts/rule units; no actual connection, transaction, graph, or model."""
from datetime import date
from types import SimpleNamespace
import json

import pytest
from pydantic import ValidationError

from erpclaw_core import masters
from erpclaw_core.common import Ctx, Refusal
import erpclaw_stock as stock
from erpclaw_stock import service
from stock_setup_fixtures import all_cases, load_text, FixtureError, SERVICES


class NoBooks:
    def execute(self, *args, **kwargs):
        raise AssertionError("No database access in stock setup units")


class OneRead:
    """One pre-supplied read response; never executes SQL or accepts a write statement."""
    def __init__(self, row):
        self.row = row

    def execute(self, query):
        assert query.is_select
        return self

    def mappings(self):
        return self

    def first(self):
        return self.row


@pytest.fixture
def ctx():
    return Ctx(NoBooks(), "company", "Test Company", "USD", date(2026, 11, 4))


@pytest.mark.parametrize("raw, expected", [
    ("MAIN WAREHOUSE.", "main warehouse"), ("  Acme   Corp. ", "acme corp"),
    ("Main\tWarehouse\nOne", "main warehouse one"), ("!!!", "")])
def test_public_name_normalisation_retains_old_alias_and_results(raw, expected):
    assert masters.normalise_name(raw) == expected
    assert masters._norm is masters.normalise_name


@pytest.mark.parametrize("service_name, args", [
    ("create_warehouse", {"name": "!!!", "account": "missing"}),
    ("make_stock_item", {"item": "missing", "method": "FIFO"}),
])
def test_setup_write_authority_precedes_even_invalid_setup_rules(ctx, service_name, args):
    definition = next(s for s in stock.SERVICES if s.name == service_name)
    with pytest.raises(Refusal) as caught:
        definition.fn(ctx, definition.input_model.model_validate(args))
    assert caught.value.code == "not_approved"


def test_create_warehouse_model_requires_the_previewed_account():
    with pytest.raises(ValidationError):
        stock.WarehouseIn.model_validate({"name": "Main Warehouse"})


@pytest.mark.parametrize("method", ["FIFO", "fifo ", "", "lifo"])
def test_method_is_never_folded_or_defaulted_and_refuses_before_item_read(ctx, method):
    with pytest.raises(Refusal) as caught:
        service.preview_make_stock_item(ctx, "missing-item", method, "missing-warehouse")
    assert caught.value.code == "bad_valuation_method"
    assert caught.value.data == {"item": "missing-item", "method": method}


@pytest.mark.parametrize("method", ["fifo", "moving_average"])
def test_new_item_preview_needs_no_item_row_and_omits_empty_default(ctx, method):
    result = service.preview_make_stock_item(ctx, None, method)
    assert result == {"method": method, "read_set": []}
    assert stock.PreviewStockItemOut.model_validate(result).model_dump() == result


def test_duplicate_warehouse_precedes_account_and_default_checks(ctx, monkeypatch):
    monkeypatch.setattr(service, "warehouses", lambda ctx: [
        {"id": "main", "name": "Main Warehouse", "is_default": True}])
    with pytest.raises(Refusal) as caught:
        service.preview_create_warehouse(ctx, "MAIN  WAREHOUSE.", "missing-account", default=True)
    assert caught.value.code == "warehouse_exists"
    assert caught.value.data == {"warehouse": "main", "name": "Main Warehouse"}


def test_preview_returns_exact_chosen_account_and_core_read_set(ctx, monkeypatch):
    monkeypatch.setattr(service, "warehouses", lambda ctx: [])
    monkeypatch.setattr(service, "_inventory_account_row",
                        lambda ctx, account, name: {"id": "inv", "number": "1300", "name": "Inventory"})
    assert service.preview_create_warehouse(ctx, "  Main   Warehouse ", "inv", default=True) == {
        "name": "Main Warehouse", "account": {"account": "inv", "number": "1300", "name": "Inventory"},
        "company_default": True, "read_set": [["account", "inv"]]}


def test_second_default_refuses_after_account_validation(ctx, monkeypatch):
    monkeypatch.setattr(service, "warehouses", lambda ctx: [
        {"id": "main", "name": "Main Warehouse", "is_default": True}])
    monkeypatch.setattr(service, "_inventory_account_row", lambda *args: {"id": "inv"})
    with pytest.raises(Refusal) as caught:
        service.preview_create_warehouse(ctx, "North Store", "inv", default=True)
    assert caught.value.code == "default_warehouse_exists"
    assert caught.value.data == {"warehouse": "main"}


@pytest.mark.parametrize("row, expected", [
    (None, "not_inventory_account"),
    ({"account_type": "bank", "is_group": False, "frozen": False}, "not_inventory_account"),
    ({"account_type": "stock", "is_group": True, "frozen": False}, "not_inventory_account"),
    ({"account_type": "stock", "is_group": False, "frozen": True,
      "number": "1300", "name": "Inventory"}, "frozen_account"),
])
def test_inventory_account_refusals_use_named_codes_and_account_data(ctx, row, expected):
    ctx.conn = OneRead(row)
    with pytest.raises(Refusal) as caught:
        service._inventory_account_row(ctx, "bad-account", "New Warehouse")
    assert caught.value.code == expected
    if expected == "not_inventory_account":
        assert caught.value.data == {"account": "bad-account"}
    else:
        assert caught.value.message == "1300 Inventory is frozen."


@pytest.mark.parametrize("asked, expected", [("fifo", "already_stock_item"),
                                            ("moving_average", "valuation_method_fixed")])
def test_existing_stock_item_refuses_before_unknown_default(ctx, monkeypatch, asked, expected):
    monkeypatch.setattr(service, "company_item", lambda ctx, item: {"id": "item", "name": "Copper Wire", "kind": "stock"})
    ctx.conn = OneRead({"method": "fifo", "default_warehouse_id": "main"})
    with pytest.raises(Refusal) as caught:
        service.preview_make_stock_item(ctx, "item", asked, "unknown")
    assert caught.value.code == expected
    assert caught.value.data["item"] == "item"
    assert caught.value.data["method"] == "fifo"
    if asked == "fifo":
        assert caught.value.data["default_warehouse"] == "main"


def test_listing_preserves_order_and_only_public_row_shape(ctx, monkeypatch):
    monkeypatch.setattr(service, "warehouses", lambda ctx: [
        {"id": "main", "name": "Main", "account_id": "inv", "is_default": True, "position": 1},
        {"id": "raw", "name": "Raw", "account_id": "inv-raw", "is_default": False, "position": 2},
    ])
    assert stock.list_warehouses(ctx, stock.NoInput()) == {"warehouses": [
        {"warehouse": "main", "name": "Main", "account": "inv", "company_default": True},
        {"warehouse": "raw", "name": "Raw", "account": "inv-raw", "company_default": False},
    ]}


def test_original_fixture_coverage_and_success_shapes_are_exact():
    cases = all_cases()
    assert len(cases) == 22
    assert {case["service"] for case in cases} == SERVICES
    definitions = {s.name: s for s in stock.SERVICES}
    assert len(definitions) == 13
    for case in cases:
        definition = definitions[case["service"]]
        definition.input_model.model_validate(case["input"])
        if "output" in case:
            assert definition.output_model.model_validate(case["output"]).model_dump() == case["output"]
    assert {case["books"]["currency"] for case in cases} == {"USD", "JPY", "KWD"}


@pytest.mark.parametrize("update", [
    {"contract": "stock/1"}, {"service": "receive_in"}, {"unexpected_number": 2},
    {"unexpected_float": 0.5}, {"output": {}, "refusal": {"code": "bad"}},
])
def test_fixture_loader_rejects_wrong_version_service_numbers_and_ambiguous_outcome(update):
    case = all_cases()[0]
    case.update(update)
    with pytest.raises(FixtureError):
        load_text(json.dumps(case))


def test_fixture_loader_rejects_missing_required_field():
    case = all_cases()[0]
    del case["input"]
    with pytest.raises(FixtureError):
        load_text(json.dumps(case))


def test_stock1_movement_account_check_keeps_its_original_frozen_behavior(ctx):
    ctx.conn = OneRead({"id": "inv", "number": "1300", "name": "Inventory",
                       "account_type": "stock", "is_group": False, "frozen": True})
    assert service._check_inventory_account(ctx, "inv", "Main Warehouse", "main") == "inv"


def test_stock1_movement_refusal_retains_warehouse_reference(ctx):
    ctx.conn = OneRead(None)
    with pytest.raises(Refusal) as caught:
        service._check_inventory_account(ctx, "missing", "Main Warehouse", "main")
    assert caught.value.code == "not_inventory_account"
    assert caught.value.data == {"warehouse": "main"}
