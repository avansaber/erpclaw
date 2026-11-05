"""Pure owner-books input boundaries; no database, graph or API fixture."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from erpclaw_core.core_contracts import AddCustomer, AddItem, SetUpCompany


@pytest.mark.parametrize("model", [SetUpCompany, AddCustomer, AddItem])
@pytest.mark.parametrize("name", ['"   "', "'   '", "“   ”", '""', "''", "   "])
def test_a_quoted_blank_name_cannot_become_a_business_record(model, name):
    with pytest.raises(ValidationError):
        model(name=name)


@pytest.mark.parametrize("model", [SetUpCompany, AddCustomer, AddItem])
def test_quoted_owner_names_keep_their_contents(model):
    assert model(name='" Lakeside Bakery "').name == "Lakeside Bakery"


@pytest.mark.parametrize("price", [0, -1, True, 1.5, "NaN", "Infinity", "0.00001"])
def test_item_prices_must_be_positive_exact_supported_amounts(price):
    with pytest.raises(ValidationError):
        AddItem(name="Consulting", unit="hour", price=price)


@pytest.mark.parametrize("price, expected", [(80, "80"), ("80.2500", "80.2500"), (Decimal("0.0001"), "0.0001")])
def test_item_price_accepts_exact_decimal_input(price, expected):
    assert AddItem(name="Consulting", unit="hour", price=price).price == Decimal(expected)


@pytest.mark.parametrize("email", ["accounts", "a b@example.com", "a@example", "a@@example.com"])
def test_customer_email_requires_one_address(email):
    with pytest.raises(ValidationError):
        AddCustomer(name="Acme", email=email)


def test_company_choices_are_not_in_the_models_tool_schema():
    props = SetUpCompany.model_json_schema()["properties"]
    assert "name" in props and "currency" in props
    assert "fiscal_year_start_month" not in props
    assert "date_order" not in props
    assert SetUpCompany(name="Acme").fiscal_year_start_month is None
    assert SetUpCompany(name="Acme").date_order is None


@pytest.mark.parametrize("unit", ["   ", "\t", "\n"])
def test_a_blank_unit_is_refused_by_existing_input_validation(unit):
    with pytest.raises(ValidationError):
        AddItem(name="Consulting", unit=unit, price="80")


def test_meaningful_unit_text_keeps_existing_whitespace_policy():
    assert AddItem(name="Consulting", unit=" per hour ", price="80").unit == "per hour"
