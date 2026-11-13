"""An email action is not a customer contact label."""
import pytest
from bookdiff import assert_changes, snapshot
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall


@pytest.mark.parametrize('model_email', ['ap@lakeside.example', None])
def test_coordinated_email_action_cannot_be_claimed_as_contact_label(engine, as_released, model_email):
    args = {'name': 'Lakeside Cafe'}
    if model_email:
        args['email'] = model_email
    assistant = Assistant(engine, ScriptedModel([Reply('', [ToolCall('call', 'add_customer', args)])]))
    before = snapshot(engine)
    events = assistant.turn('action', 'Add a customer called Lakeside Cafe and email ap@lakeside.example')
    assert [e['type'] for e in events] == ['ask'] and events[0]['code'] == 'changes_ambiguous', events
    assert_changes(before, snapshot(engine), {})


@pytest.mark.parametrize('said', [
    'Add a customer called Lakeside Cafe, email ap@lakeside.example',
    'Add a customer called Lakeside Cafe with email ap@lakeside.example',
    'Add a customer called "Lakeside and email ap@lakeside.example"',
])
def test_contact_labels_and_quoted_names_do_not_become_extra_actions(said):
    from erpclaw_assistant.resolve import write_intents
    assert write_intents(said) <= 1
