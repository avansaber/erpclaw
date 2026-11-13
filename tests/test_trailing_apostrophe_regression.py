"""Apostrophes belonging to values are not message wrappers."""
import pytest
from erpclaw_assistant.graph import Assistant, _reading_text
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall, call


@pytest.mark.parametrize('said', ["Add a customer called O'Brien'", "Add a customer called Smiths'",
                                  "Hooli paid 100, reference Smiths'"])
def test_trailing_possessive_apostrophe_is_not_stripped_from_value(said):
    assert _reading_text(said) == said


@pytest.mark.parametrize('name', ["O'Brien'", "Smiths'"])
def test_customer_name_keeps_trailing_apostrophe_on_card(engine, name):
    assistant = Assistant(engine, ScriptedModel([Reply('', [ToolCall('call', 'add_customer', {'name': name})])]))
    events = assistant.turn('apostrophe', f'Add a customer called {name}')
    assert [e['type'] for e in events] == ['card'], events
    assert ['Name', name] in events[0]['card']['lines']


def test_payment_reference_keeps_trailing_apostrophe_on_card(engine):
    assistant = Assistant(engine, ScriptedModel([Reply('', [call('record_payment', customer='Hooli', amount=100, reference="Smiths'")])]))
    events = assistant.turn('memo', "Hooli paid 100, reference Smiths'")
    assert [e['type'] for e in events] == ['card'], events
    assert ['Reference', "Smiths'"] in events[0]['card']['lines']
