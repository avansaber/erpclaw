import pytest

from card_reach_support import card, invoice


@pytest.mark.parametrize('wording', [
    'invoice Hooli for 5 Copper Wire or 2 Circuit Board and 3 Motor Housing Unit',
    'invoice Hooli for 3 Motor Housing Unit and 5 Copper Wire or 2 Circuit Board',
])
def test_choice_preserves_separate_line(engine, wording):
    a = invoice(engine, 'Hooli', [
        {'item': 'Copper Wire', 'quantity': 5},
        {'item': 'Motor Housing Unit', 'quantity': 3},
    ])
    question = a.turn('choice-lines', wording)[-1]
    assert question['type'] == 'ask', question
    result = a.turn('choice-lines', 'Copper Wire')
    c = card(result)
    assert 'Copper Wire' in str(c) and 'Motor Housing Unit' in str(c), c
    assert 'Circuit Board' not in str(c), c
    assert "5 x Copper Wire" in str(c["card"]["lines"]) and "3 x Motor Housing Unit" in str(c["card"]["lines"]), c


def test_missing_item_does_not_suggest_instruction_words(engine):
    a = invoice(engine, 'Hooli', [{'item': None, 'quantity': 1}])
    question = a.turn('missing-item', 'invoice Hooli and include the unit')[-1]
    assert question['type'] == 'ask' and question['code'] == 'item_missing', question
    assert 'Motor Housing Unit' not in str(question), question
