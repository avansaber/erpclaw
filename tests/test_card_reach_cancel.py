import pytest
from erpclaw_assistant import store
from card_reach_support import invoice


@pytest.mark.parametrize('word', ['Cancel', 'stop', 'never mind'])
def test_cancel_open_request(engine, word):
    a = invoice(engine, 'Hooli', [{'item': 'copper', 'quantity': 5}])
    assert a.turn('cancel', 'invoice Hooli for 5 copper')[-1]['type'] == 'ask'
    result = a.turn('cancel', word)
    assert result == [{'type': 'answer', 'text': 'Cancelled; nothing was saved.', 'checked': 'engine', 'currency': 'USD'}], result
    assert store.open_request('cancel') is None
