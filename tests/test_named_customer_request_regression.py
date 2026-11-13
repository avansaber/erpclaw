"""A complete named request replaces a pending question."""
from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall
from erpclaw_core import masters
from erpclaw_core.common import company_ctx


def test_with_name_starts_new_request_instead_of_repeating_question(engine):
    with engine.begin() as conn:
        masters.create_party(company_ctx(conn), 'supplier', 'Copperline Metals')
    first = Assistant(engine, ScriptedModel([Reply('', [ToolCall('call', 'add_supplier', {'name': 'Copperline Metal'})])]))
    assert first.turn('named', 'Add a supplier called Copperline Metal')[-1]['type'] == 'ask'
    old = store.open_request('named')['id']
    second = Assistant(engine, ScriptedModel([Reply('', [ToolCall('call', 'add_customer', {'name': 'Lakeside Cafe'})])]))
    events = second.turn('named', 'Add a customer with name Lakeside Cafe')
    assert store.get_request(old)['ended_why'] == 'set_aside'
    assert [event['type'] for event in events] == ['card'], events
    assert ['Name', 'Lakeside Cafe'] in events[0]['card']['lines']
