"""Contact values are saved without their wrapping quotes."""
import pytest
from sqlalchemy import select
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall
from erpclaw_core import approvals, models as m


@pytest.mark.parametrize('quote', ['"', "'", '“'])
def test_wrapping_contact_quotes_do_not_reach_card_or_saved_party(engine, quote):
    end = '”' if quote == '“' else quote
    email = 'ap@lakeside.example'
    text = f'Add a customer called Lakeside Cafe, email {quote}{email}{end}'
    assistant = Assistant(engine, ScriptedModel([Reply('', [ToolCall('call', 'add_customer', {'name': 'Lakeside Cafe', 'email': email})])]))
    [card] = assistant.turn('quoted', text)
    assert card['type'] == 'card', card
    assert ['Email', email] in card['card']['lines']
    assert approvals.approve(engine, card['approval_id'], card['digest']).status == 'committed'
    with engine.connect() as conn:
        saved = conn.execute(select(m.party.c.email).where(m.party.c.name == 'Lakeside Cafe')).scalar_one()
    assert saved == email


def test_wrapping_city_quotes_do_not_reach_customer_card(engine):
    operation = ToolCall('call', 'add_customer', {'name': 'Lakeside Cafe', 'city': 'New York'})
    assistant = Assistant(engine, ScriptedModel([Reply('', [operation])]))
    [card] = assistant.turn('city', 'Add a customer called Lakeside Cafe with city "New York"')
    assert card['type'] == 'card', card
    assert ['City', 'New York'] in card['card']['lines']
