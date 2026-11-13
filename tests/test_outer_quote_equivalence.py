"""Boundary quotes have no effect on the owner's request."""
import pytest
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall


def run(engine, cid, text):
    operation = ToolCall('call', 'add_customer', {'name': 'Lakeside Cafe', 'email': 'ap@lakeside.example'})
    return Assistant(engine, ScriptedModel([Reply('', [operation])])).turn(cid, text)


def canonical(events):
    return [{k: v for k, v in event.items() if k not in {'approval_id', 'digest', 'steps'}} for event in events]


@pytest.mark.parametrize('text', [
    '"Add a customer called Lakeside Cafe, ap@lakeside.example"',
    '"Add a customer called Lakeside Cafe, ap@lakeside.example',
    'Add a customer called Lakeside Cafe, ap@lakeside.example"',
    "'Add a customer called Lakeside Cafe, ap@lakeside.example'",
    "'Add a customer called Lakeside Cafe, ap@lakeside.example",
    "Add a customer called Lakeside Cafe, ap@lakeside.example'",
])
def test_outer_quotes_match_unquoted_request(engine, text):
    bare = run(engine, 'bare', 'Add a customer called Lakeside Cafe, ap@lakeside.example')
    quoted = run(engine, 'quoted', text)
    assert bare[-1]['type'] == 'card', bare
    assert canonical(quoted) == canonical(bare), (bare, quoted)


def test_inner_quotes_are_kept(engine):
    said = 'Add a customer called "Lakeside Cafe" with city York'
    operation = ToolCall('call', 'add_customer', {'name': 'Lakeside Cafe', 'city': 'York'})
    model = ScriptedModel([Reply('', [operation])])
    result = Assistant(engine, model).turn('inner', said)
    assert result[-1]['type'] == 'card', result
    assert ['Name', 'Lakeside Cafe'] in result[-1]['card']['lines']
    assert any(said in str(message) for message in model.seen[0])
