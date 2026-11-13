import pytest
from card_reach_support import invoice, card


@pytest.mark.parametrize('customer,item,quantity', [('Hooli', 'copper wire', 5), ('Initech', 'circuit board', 2)])
def test_exact_item_does_not_suggest_another(engine, customer, item, quantity):
    a = invoice(engine, customer, [{'item': item, 'quantity': quantity}])
    card(a.turn('exact', f'invoice {customer} for {quantity} {item}'))


def test_chosen_item_is_not_replaced(engine):
    a = invoice(engine, 'Hooli', [{'item': 'copper', 'quantity': 5}])
    q = a.turn('pick', 'invoice Hooli for 5 copper')[-1]
    assert q['type'] == 'ask' and 'Copper Wire' in q['options'], q
    result = a.turn('pick', 'Copper Wire')
    assert 'Motor Housing Unit' not in str(result), result
    card(result)


def test_near_candidates_resemble_the_name(engine):
    a = invoice(engine, 'Hooli', [{'item': 'circut board', 'quantity': 2}])
    q = a.turn('near', 'invoice Hooli for 2 circut board')[-1]
    assert q['type'] == 'ask', q
    assert 'Motor Housing Unit' not in q['options'], q
