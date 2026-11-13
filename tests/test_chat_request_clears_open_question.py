"""A complete request starts over, while a value still answers a question."""
from bookdiff import assert_changes, snapshot
from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall
from erpclaw_core import masters
from erpclaw_core.common import company_ctx


def turn(engine, cid, text, operation, args):
    return Assistant(engine, ScriptedModel([Reply('', [ToolCall('call', operation, args)])] * 3)).turn(cid, text)


def open_supplier(engine):
    with engine.begin() as conn:
        masters.create_party(company_ctx(conn), 'supplier', 'Copperline Metals')
    events = turn(engine, 'fresh', 'Add a supplier called Copperline Metal', 'add_supplier', {'name': 'Copperline Metal'})
    assert events[-1]['type'] == 'ask', events
    return store.open_request('fresh')['id']


def test_complete_invoice_is_its_own_request(engine):
    old = open_supplier(engine)
    before = snapshot(engine)
    events = turn(engine, 'fresh', 'Create an invoice for Hooli: 5 Copper Wire', 'create_invoice',
                  {'customer': 'Hooli', 'items': [{'item': 'Copper Wire', 'quantity': 5}]})
    assert store.get_request(old)['ended_why'] == 'set_aside'
    assert events[-1]['type'] == 'card', events
    assert_changes(before, snapshot(engine), {'approval': {'added': [{'id': events[-1]['approval_id'], 'status': 'pending'}]}})


def test_complete_item_request_gets_its_own_question(engine):
    old = open_supplier(engine)
    before = snapshot(engine)
    events = turn(engine, 'fresh', 'Add an item called Consulting, per hour at $80', 'add_item',
                  {'name': 'Consulting', 'unit': 'hour', 'price': '80'})
    assert store.get_request(old)['ended_why'] == 'set_aside'
    assert events[-1]['type'] == 'ask' and events[-1]['code'] == 'item_kind_missing', events
    assert_changes(before, snapshot(engine), {})


def test_quantity_value_still_answers_the_open_question(engine):
    first = turn(engine, 'value', 'Invoice Hooli for Copper Wire', 'create_invoice',
                 {'customer': 'Hooli', 'items': [{'item': 'Copper Wire'}]})
    assert first[-1]['code'] == 'quantity_missing', first
    old = store.open_request('value')['id']
    events = turn(engine, 'value', '5', 'create_invoice',
                  {'customer': 'Hooli', 'items': [{'item': 'Copper Wire', 'quantity': 5}]})
    assert store.open_request('value')['id'] == old
    assert events[-1]['type'] == 'card', events


def test_supplier_replaces_an_unplaced_email_question(engine):
    before = snapshot(engine)
    asked = turn(engine, 'email-question', 'Add a customer called Lakeside Cafe, email ap@lakeside.example',
                 'add_customer', {'name': 'Lakeside Cafe'})
    assert asked[-1]['type'] == 'ask' and asked[-1]['code'] == 'email_ambiguous', asked
    old = store.open_request('email-question')['id']
    assert_changes(before, snapshot(engine), {})
    events = turn(engine, 'email-question', 'Add a supplier called Acme Parts', 'add_supplier', {'name': 'Acme Parts'})
    assert store.get_request(old)['ended_why'] == 'set_aside'
    assert [e['type'] for e in events] == ['card'], events
    assert events[0]['card']['title'] == 'Add a supplier'
    assert_changes(before, snapshot(engine), {'approval': {'added': [{'id': events[0]['approval_id'], 'status': 'pending'}]}})


def test_incomplete_operation_does_not_set_aside_question(engine):
    old = open_supplier(engine)
    before = snapshot(engine)
    events = turn(engine, 'fresh', 'Add a customer', 'add_customer', {'name': 'Lakeside Cafe'})
    assert events[-1]['type'] == 'ask' and events[-1]['code'] == 'supplier_near_ambiguous', events
    assert store.open_request('fresh')['id'] == old
    assert_changes(before, snapshot(engine), {})


def test_complete_named_document_action_starts_its_own_request(engine):
    old = open_supplier(engine)
    before = snapshot(engine)
    events = turn(engine, 'fresh', 'Cancel INV-2026-00003, it was a mistake', 'cancel_invoice',
                  {'invoice': 'INV-2026-00003'})
    assert store.get_request(old)['ended_why'] == 'set_aside'
    assert events[-1]['type'] == 'card', events
    assert_changes(before, snapshot(engine), {'approval': {'added': [{'id': events[-1]['approval_id'], 'status': 'pending'}]}})


def test_complete_read_starts_its_own_request(engine):
    old = open_supplier(engine)
    before = snapshot(engine)
    events = turn(engine, 'fresh', 'Show sales', 'get_metric', {'metric': 'sales'})
    assert store.get_request(old)['ended_why'] == 'set_aside'
    assert events[-1]['type'] == 'answer', events
    assert_changes(before, snapshot(engine), {})
