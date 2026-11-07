"""Result money uses the same kinds and exact formatting as approval cards."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from erpclaw_core import approvals, models as m
from erpclaw_core.common import Refusal
from erpclaw_core.contracts import Amount, Contract, Input, Result, amount_dicts, check_answer
from erpclaw_core.plugins import REGISTRY


@pytest.fixture
def contract():
    return Contract("price", "core", "read", "Show a price.", Input, amounts=("value", "amount"))


def test_result_rate_preserves_the_card_precision(contract):
    amount = Amount("value", "Consulting", "12.3456", "USD", kind="unit_price")
    result = Result("Consulting costs 12.3456 USD per hour.", amounts=[amount])

    expected = [{"what": "value", "of": "Consulting", "value": "12.3456", "currency": "USD",
                 "kind": "unit_price"}]
    assert check_answer(contract, result) == expected
    assert amount_dicts(result.amounts) == expected


def test_result_rate_cannot_be_rounded_to_currency_precision(contract):
    result = Result("Consulting costs 12.35 USD per hour.", amounts=[
        Amount("value", "Consulting", "12.3456", "USD", kind="unit_price")
    ])

    with pytest.raises(Refusal) as caught:
        check_answer(contract, result)
    assert caught.value.code == "answer_amounts"


def test_result_amount_defaults_to_currency_precision_and_keeps_its_public_shape(contract):
    amount = Amount("amount", None, "345.00", "USD")
    assert amount.kind == "amount"
    result = Result("The value is 345.00 USD.", amounts=[amount])
    expected = [{"what": "amount", "of": None, "value": "345.00", "currency": "USD"}]

    assert check_answer(contract, result) == expected
    assert amount_dicts(result.amounts) == expected
    explicit = Amount("amount", None, "345.00", "USD", kind="amount")
    assert amount_dicts([explicit]) == expected


def test_result_amount_without_kind_still_requires_currency_decimals(contract):
    result = Result("The value is 345 USD.", amounts=[Amount("amount", None, "345.00", "USD")])

    with pytest.raises(Refusal) as caught:
        check_answer(contract, result)
    assert caught.value.code == "answer_amounts"


def test_result_unknown_amount_kind_is_refused(contract):
    result = Result("The value is 345.00 USD.", amounts=[
        Amount("amount", None, "345.00", "USD", kind="unknown")
    ])

    with pytest.raises(Refusal) as caught:
        check_answer(contract, result)
    assert caught.value.code == "answer_amounts"


@pytest.mark.parametrize("text, accepted", [
    ("12.3456 USD per hour; total 345.00 USD.", True),
    ("345.00 USD total; 12.3456 USD per hour.", False),
    ("12.3456 USD per hour.", False),
])
def test_result_mixed_kinds_require_every_figure_in_declared_order(contract, text, accepted):
    result = Result(text, amounts=[Amount("value", None, "12.3456", "USD", kind="unit_price"),
                                   Amount("amount", None, "345.00", "USD")])

    if accepted:
        assert check_answer(contract, result) == amount_dicts(result.amounts)
    else:
        with pytest.raises(Refusal) as caught:
            check_answer(contract, result)
        assert caught.value.code == "answer_amounts"


def test_add_item_commit_states_and_declares_its_stored_rate(engine):
    contract = REGISTRY.contracts["add_item"]
    card = approvals.propose(engine, contract, {
        "name": "Consulting", "unit": "hour", "price": "12.3456", "kind": "service"
    })
    assert ["Selling price", "12.3456 USD per hour"] in card.card["lines"]
    assert [a for a in card.card["amounts"] if a["label"] == "Selling price"] == [{
        "where": "lines", "index": next(i for i, row in enumerate(card.card["lines"])
                                        if row[0] == "Selling price"),
        "label": "Selling price", "kind": "unit_price", "value": "12.3456", "currency": "USD"
    }]

    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == "committed", result.text
    assert result.text == "I added Consulting at 12.3456 USD per hour."
    assert contract.amounts == ("rate",)
    assert result.amounts == [{"what": "rate", "of": "Consulting", "value": "12.3456",
                               "currency": "USD", "kind": "unit_price"}]
    assert result.figures == ["12.3456"]
    assert check_answer(contract, Result(result.text, amounts=[Amount(**a) for a in result.amounts])) == result.amounts

    with engine.connect() as conn:
        item = conn.execute(select(m.item).where(m.item.c.name == "Consulting")).mappings().one()
    assert item["standard_rate"] == Decimal("12.3456")
    assert item["unit"] == "hour"
    read_back = approvals.read_back(engine, result.operation_key)
    assert read_back.text == result.text
    assert read_back.amounts == result.amounts
