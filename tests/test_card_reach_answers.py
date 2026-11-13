from card_reach_support import assistant, invoice, card


def test_service_kind_answer_reaches_card(engine):
    a = assistant(engine, 'add_item', name='Cloud Design', unit='hour', price='80')
    q = a.turn('service', 'Add an item called Cloud Design, per hour at $80')[-1]
    assert q['type'] == 'ask' and q['code'] == 'item_kind_missing', q
    c = card(a.turn('service', 'A service I sell'))
    assert 'Cloud Design' in str(c), c


def test_numbered_item_choice_is_taken(engine):
    a = invoice(engine, 'Hooli', [{'item': 'copper', 'quantity': 5}])
    q = a.turn('number', 'invoice Hooli for 5 copper')[-1]
    assert q['type'] == 'ask' and q['options'][0] == 'Copper Wire', q
    card(a.turn('number', '1'))


def test_service_kind_survives_another_question(engine):
    from erpclaw_assistant.graph import Assistant
    from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall

    first = ToolCall('call', 'add_item', {'name': 'Cloud Design', 'price': '80'})
    priced = ToolCall('call', 'add_item', {'name': 'Cloud Design', 'unit': 'hour', 'price': '80'})
    a = Assistant(engine, ScriptedModel([Reply('', [first]), Reply('', [first]), Reply('', [priced])]))
    q = a.turn('service-price', 'Add an item called Cloud Design at $80')[-1]
    assert q['code'] == 'item_kind_missing', q
    second = a.turn('service-price', 'A service I sell')[-1]
    assert second['type'] == 'ask' and second['code'] == 'item_unit_missing', second
    card(a.turn('service-price', 'hour'))
