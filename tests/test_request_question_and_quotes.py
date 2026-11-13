"""Fresh requests, pending answers and punctuation at message boundaries."""
from bookdiff import assert_changes, snapshot
from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall
from erpclaw_core import masters, models as m
from erpclaw_core.common import company_ctx


def turn(engine, cid, text, operation, **args):
    model = ScriptedModel([Reply('', [ToolCall('call', operation, args)])] * 4)
    return Assistant(engine, model).turn(cid, text)


def near_supplier(engine):
    with engine.begin() as conn:
        masters.create_party(company_ctx(conn), 'supplier', 'Copperline Metals', city='Leeds')
    return turn(engine, 'chat', 'Add a supplier called Copperline Metal', 'add_supplier', name='Copperline Metal')


def test_new_complete_request_sets_question_aside(engine):
    asked = near_supplier(engine)
    assert asked[-1]['type'] == 'ask', asked
    old = store.open_request('chat')['id']
    before = snapshot(engine)
    result = turn(engine, 'chat', 'Add a customer called Lakeside Cafe', 'add_customer', name='Lakeside Cafe')
    assert [e['type'] for e in result] == ['card'], result
    assert result[0]['card']['title'] == 'Add a customer'
    assert store.get_request(old)['state'] == 'ended'
    assert_changes(before, snapshot(engine), {'approval': {'added': [{'id': result[0]['approval_id'], 'status': 'pending'}]}})


def test_choice_answer_still_binds(engine):
    asked = near_supplier(engine)
    choice = next(c for c in asked[-1]['choices'] if 'new supplier' in c['label'])
    result = turn(engine, 'chat', choice['label'], 'add_supplier', name='Copperline Metal')
    assert result[-1]['type'] == 'card', result
    assert ['Name', 'Copperline Metal'] in result[-1]['card']['lines']


def test_trailing_quote_with_email(engine):
    result = turn(engine, 'quote', 'Add a customer called Lakeside Cafe, email ap@lakeside.example"',
                  'add_customer', name='Lakeside Cafe', email='ap@lakeside.example')
    assert [e['type'] for e in result] == ['card'], result
    assert ['Email', 'ap@lakeside.example'] in result[0]['card']['lines']


def test_contact_label_with_email(engine):
    result = turn(engine, 'email', 'Add a customer called Lakeside Cafe, email ap@lakeside.example',
                  'add_customer', name='Lakeside Cafe', email='ap@lakeside.example')
    assert [e['type'] for e in result] == ['card'], result
    assert ['Email', 'ap@lakeside.example'] in result[0]['card']['lines']


def test_same_operation_with_new_name_sets_question_aside(engine):
    near_supplier(engine)
    before = snapshot(engine)
    result = turn(engine, 'chat', 'Add a supplier called Acme Parts', 'add_supplier', name='Acme Parts')
    assert [e['type'] for e in result] == ['card'], result
    assert ['Name', 'Acme Parts'] in result[0]['card']['lines']
    assert_changes(before, snapshot(engine), {'approval': {'added': [{'id': result[0]['approval_id'], 'status': 'pending'}]}})


def test_short_answer_does_not_set_question_aside(engine):
    asked = near_supplier(engine)
    old = store.open_request('chat')['id']
    before = snapshot(engine)
    result = turn(engine, 'chat', 'hello', 'add_customer', name='Lakeside Cafe')
    assert result == asked
    assert store.open_request('chat')['id'] == old
    assert_changes(before, snapshot(engine), {})


def test_owner_sequence_with_fresh_books_names(engine):
    # The demo includes Hooli; move its seeded name so these words describe a new customer.
    with engine.begin() as conn:
        conn.execute(m.party.update().where(m.party.c.name == 'Hooli').values(name='Existing Demo Customer'))
    invoice = {'customer': 'Hooli', 'items': [{'item': 'Widgets', 'quantity': 4}]}
    before = snapshot(engine)
    for text, operation, args in [
        ('Create an invoice for Hooli: 4 Widgets at $25', 'create_invoice', invoice),
        ('widgets', 'create_invoice', invoice),
        ('then add', 'add_item', {'name': 'widgets'}),
        ('customer', 'add_item', {'name': 'widgets'}),
        ('4 is quantity', 'add_item', {'name': 'widgets'}),
        ('each', 'add_item', {'name': 'widgets', 'unit': 'each'}),
    ]:
        turn(engine, 'sequence', text, operation, **args)
    assert_changes(before, snapshot(engine), {})
    events = turn(engine, 'sequence', 'Add a customer called Hooli, email ap@hooli.example"',
                  'add_customer', name='Hooli', email='ap@hooli.example')
    assert [e['type'] for e in events] == ['card'], events
    assert ['Email', 'ap@hooli.example'] in events[0]['card']['lines']
    events = turn(engine, 'sequence', 'Add a supplier called Acme Parts', 'add_supplier', name='Acme Parts')
    assert any(e['type'] == 'card' and e['card']['title'] == 'Add a supplier' for e in events), events
