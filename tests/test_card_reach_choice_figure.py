import pytest
from card_reach_support import invoice, card


def test_choice_number_is_not_an_unread_figure_on_later_turn(engine):
    a = invoice(engine, 'Hooli', [{'item': 'copper', 'quantity': 5}])
    q = a.turn('number-later', 'invoice Hooli for 5 copper at $35')[-1]
    assert q['type'] == 'ask' and q['options'][0] == 'Copper Wire', q
    second = a.turn('number-later', '1')
    c = card(second)
    assert '@ 35.00 = 175.00' in str(c['card']['lines']), c


@pytest.mark.parametrize('choice, item', [('1', 'Circuit Board'), ('2', 'Copper Wire')])
def test_numbered_alternative_is_consumed(engine, choice, item):
    a = invoice(engine, 'Hooli', [{'item': 'Copper Wire', 'quantity': 5}])
    q = a.turn('second-choice', 'invoice Hooli for 5 Copper Wire or Circuit Board')[-1]
    assert q['type'] == 'ask' and q['options'] == ['Circuit Board', 'Copper Wire'], q
    result = a.turn('second-choice', choice)
    if choice == '1':
        assert result[-1]['code'] == 'quantity_missing', result
        result = a.turn('second-choice', '5')
    c = card(result)
    assert item in str(c), c


def test_choice_does_not_consume_a_separate_item_occurrence(engine):
    a = invoice(engine, 'Hooli', [{'item': 'Copper Wire', 'quantity': 5}])
    first = a.turn('extra-item', 'invoice Hooli for 5 Copper Wire or Circuit Board and Circuit Board')[-1]
    assert first['type'] == 'ask' and first['options'] == ['Circuit Board', 'Copper Wire'], first
    result = a.turn('extra-item', '2')
    assert result[-1]['type'] == 'ask', result


@pytest.mark.parametrize('later_name', ['coppr', 'Copper Wire'])
def test_chosen_misspelling_keeps_its_price(engine, later_name):
    from erpclaw_assistant.graph import Assistant
    from erpclaw_assistant.model import Reply, ScriptedModel, call

    first_call = call('create_invoice', customer='Hooli', items=[{'item': 'coppr', 'quantity': 5}])
    later_call = call('create_invoice', customer='Hooli', items=[{'item': later_name, 'quantity': 5}])
    a = Assistant(engine, ScriptedModel([Reply('', [first_call])] + [Reply('', [later_call])] * 2))
    first = a.turn('misspelling', 'invoice Hooli for 5 coppr at $35')[-1]
    assert first['type'] == 'ask' and first['options'][0] == 'Copper Wire', first
    second = a.turn('misspelling', '1')
    c = card(second)
    assert '@ 35.00 = 175.00' in str(c['card']['lines']), c


def test_chosen_misspelling_with_list_price_reaches_card(engine):
    a = invoice(engine, 'Hooli', [{'item': 'coppr', 'quantity': 5}])
    first = a.turn('misspelling-list', 'invoice Hooli for 5 coppr at $28')[-1]
    assert first['type'] == 'ask' and first['options'][0] == 'Copper Wire', first
    card(a.turn('misspelling-list', '1'))
