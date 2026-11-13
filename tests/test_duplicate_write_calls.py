"""Several model calls for one owner request must never choose money silently."""
import pytest
from bookdiff import snapshot
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call

SAID = 'Record a 400.00 payment from Pied Piper on INV-2026-00008.'


def run(engine, calls, said=SAID):
    before = snapshot(engine)
    events = Assistant(engine, ScriptedModel([Reply('', calls)])).turn('duplicate-calls', said)
    assert snapshot(engine) == before
    return events


def payment(**overrides):
    return call('record_payment', **({'customer': 'Pied Piper', 'amount': 400, 'invoice': 'INV-2026-00008'} | overrides))


def test_identical_calls(engine):
    events = run(engine, [payment(), payment()])
    assert len(events) == 1 and events[0]['type'] in ('card', 'ask'), events
    assert events[0].get('code') != 'changes_ambiguous', events


def test_equivalent_amount_representations(engine):
    events = run(engine, [payment(), payment(amount='400.00')])
    assert len(events) == 1 and events[0]['type'] in ('card', 'ask'), events
    assert events[0].get('code') != 'changes_ambiguous', events


def test_invoice_number_is_not_a_duplicate_payment_amount(engine):
    events = run(engine, [payment(amount=8), payment(amount=8)])
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    assert events[0]['code'] == 'amount_missing', events


@pytest.mark.parametrize('field,value,expected', [
    ('amount', 40, ('400', '40')),
    ('invoice', 'INV-2026-00007', ('INV-2026-00008', 'INV-2026-00007')),
])
def test_different_payment_values_ask(engine, field, value, expected):
    events = run(engine, [payment(), payment(**{field: value})])
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    assert all(v in events[0]['text'] for v in expected), events


def test_different_contracts_ask(engine):
    events = run(engine, [payment(), call('cancel_invoice', invoice='INV-2026-00008')])
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    assert 'payment' in events[0]['text'].lower() and 'cancel' in events[0]['text'].lower(), events
    assert events[0]['text'].startswith('I can only do one step at a time here.'), events


def test_two_owner_changes_keep_existing_reply(engine):
    events = run(engine, [payment(), call('cancel_invoice', invoice='INV-2026-00008')],
                 'Record a 400 payment from Pied Piper and cancel INV-2026-00008.')
    assert len(events) == 1 and events[0]['code'] == 'changes_ambiguous', events
    assert events[0]['text'] == 'One change at a time, please: send each change as its own message.'


def test_customer_capitalization_resolves_to_same_customer(engine):
    events = run(engine, [payment(customer='pied piper'), payment()])
    assert len(events) == 1 and events[0]['type'] in ('card', 'ask'), events
    assert events[0].get('code') != 'changes_ambiguous', events


def test_customer_capitalization_does_not_hide_amount_conflict(engine):
    events = run(engine, [payment(customer='pied piper'), payment(amount='40.00')])
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    assert all(value in events[0]['text'] for value in ('400', '40')), events


def test_invoice_suffix_resolves_to_same_document(engine):
    events = run(engine, [payment(invoice='8'), payment()])
    assert len(events) == 1 and events[0]['type'] in ('card', 'ask'), events
    assert events[0].get('code') != 'changes_ambiguous', events


def test_same_customer_different_invoice_quantities_ask_plainly(engine):
    calls = [call('create_invoice', customer='Acme Corporation',
                  items=[{'item': 'copper wire', 'quantity': quantity}]) for quantity in (5, 7)]
    events = run(engine, calls, 'Invoice Acme Corporation for 5 copper wire.')
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    text = events[0]['text']
    assert all(value in text for value in ('5', '7', 'copper wire')), events
    assert not any(raw in text for raw in ('{', '[', '"quantity"', 'items', 'quantity')), events


def test_different_customers_are_two_changes(engine):
    calls = [call('create_invoice', customer=customer, items=[{'item': item, 'quantity': quantity}])
             for customer, item, quantity in [('Acme Corporation', 'copper wire', 5),
                                              ('Hooli', 'circuit board', 2)]]
    events = run(engine, calls, 'invoice Acme Corporation for 5 copper wire and Hooli for 2 circuit boards')
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    assert events[0]['text'] == 'One change at a time, please: send each change as its own message.'


def test_close_payment_amounts_are_shown_exactly(engine):
    events = run(engine, [payment(amount='100.004'), payment(amount='100.001')],
                 'Record a 100.004 payment from Pied Piper on INV-2026-00008, not 2 percent.')
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    assert all(value in events[0]['text'] for value in ('100.004', '100.001')), events


def test_payment_method_and_reference_have_distinct_labels(engine):
    events = run(engine, [payment(method='cash', reference='alpha'),
                          payment(method='check', reference='beta')])
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    text = events[0]['text'].lower()
    assert all(part in text for part in ('payment method: cash', 'payment method: check',
                                         'reference: alpha', 'reference: beta')), events


def test_invoice_line_item_and_quantity_have_labels(engine):
    calls = [call('create_invoice', customer='Acme Corporation',
                  items=[{'item': item, 'quantity': quantity}])
             for item, quantity in [('copper wire', 5), ('circuit board', 7)]]
    events = run(engine, calls, 'Invoice Acme Corporation for 5 copper wire, not 2 percent.')
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    text = events[0]['text'].lower()
    assert all(part in text for part in ('item: copper wire', 'item: circuit board',
                                         'number: 5', 'number: 7')), events
    assert not any(raw in text for raw in ('{', '[', '"quantity"', 'items')), events


@pytest.mark.parametrize('contract,base,field,first,second,expected', [
    ('create_invoice', {'customer': 'Acme Corporation'}, 'items',
     {'item': 'copper wire', 'quantity': 5}, {'item': 'circuit board', 'quantity': 7},
     ('item: copper wire', 'item: circuit board', 'number: 5', 'number: 7')),
    ('create_sales_order', {'customer': 'Acme Corporation'}, 'items',
     {'item': 'copper wire', 'quantity': 5}, {'item': 'circuit board', 'quantity': 7},
     ('item: copper wire', 'item: circuit board', 'number: 5', 'number: 7')),
    ('deliver_order', {'order': 'SO-2026-00001'}, 'items',
     {'item': 'copper wire', 'quantity': 5, 'warehouse': 'Main'},
     {'item': 'circuit board', 'quantity': 7, 'warehouse': 'West'},
     ('item: copper wire', 'item: circuit board', 'number: 5', 'number: 7', 'warehouse: main', 'warehouse: west')),
    ('create_credit_note', {'invoice': 'INV-2026-00008'}, 'items',
     {'item': 'copper wire', 'quantity': 5, 'line': 1}, {'item': 'circuit board', 'quantity': 7, 'line': 2},
     ('item: copper wire', 'item: circuit board', 'number: 5', 'number: 7', 'line: 1', 'line: 2')),
    ('record_opening_stock', {}, 'lines',
     {'item': 'copper wire', 'warehouse': 'Main', 'quantity': '5', 'rate': '1.2345'},
     {'item': 'circuit board', 'warehouse': 'West', 'quantity': '7', 'rate': '2.3456'},
     ('item: copper wire', 'item: circuit board', 'warehouse: main', 'warehouse: west', 'number: 5', 'number: 7', 'rate: 1.2345', 'rate: 2.3456')),
    ('record_stock_count', {}, 'lines',
     {'item': 'copper wire', 'warehouse': 'Main', 'counted': '5', 'rate': '1.2345'},
     {'item': 'circuit board', 'warehouse': 'West', 'counted': '7', 'rate': '2.3456'},
     ('item: copper wire', 'item: circuit board', 'warehouse: main', 'warehouse: west', 'counted: 5', 'counted: 7', 'rate: 1.2345', 'rate: 2.3456')),
    ('record_opening_position', {}, 'bank_lines',
     {'account': 'Bank', 'amount': '100.004'}, {'account': 'Cash', 'amount': '200.005'},
     ('account: bank', 'account: cash', 'amount: 100.004', 'amount: 200.005')),
    ('record_opening_position', {}, 'supplier_lines',
     {'supplier': 'Acme', 'amount': '100.004', 'old_reference': 'old A', 'due_date': '2026-12-01'},
     {'supplier': 'Hooli', 'amount': '200.005', 'old_reference': 'old B', 'due_date': '2026-12-02'},
     ('supplier: acme', 'supplier: hooli', 'amount: 100.004', 'amount: 200.005', 'old reference: old a', 'old reference: old b', 'due date: 2026-12-01', 'due date: 2026-12-02')),
])
def test_nested_conflicts_show_declared_fields(engine, monkeypatch, contract, base, field, first, second, expected):
    import erpclaw_assistant.graph as graph
    original = graph.enabled_plugins
    monkeypatch.setattr(graph, 'enabled_plugins', lambda conn: original(conn) | {'stock'})
    calls = [call(contract, **(base | {field: [row]})) for row in (first, second)]
    events = run(engine, calls, 'Please handle the requested figures, not 2 percent.')
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    text = events[0]['text'].lower()
    assert all(part in text for part in expected), events
    assert not any(part in text for part in ('not specified', '{', '}', '[', ']', '"', 'bank_lines', 'supplier_lines', 'old_reference', 'due_date')), events


@pytest.mark.parametrize('contract,field,row,expected', [
    ('record_opening_position', 'bank_lines', {'account': 'Bank', 'amount': '100.00'},
     ('bank balances: none', 'account: bank', 'amount: 100')),
    ('record_stock_count', 'lines',
     {'item': 'copper wire', 'warehouse': 'Main', 'counted': '5', 'rate': '1.25'},
     ('lines: none', 'item: copper wire', 'counted: 5', 'rate: 1.25')),
])
def test_explicit_empty_list_conflicts_with_populated_list(engine, monkeypatch, contract, field, row, expected):
    import erpclaw_assistant.graph as graph
    original = graph.enabled_plugins
    monkeypatch.setattr(graph, 'enabled_plugins', lambda conn: original(conn) | {'stock'})
    calls = [call(contract, **{field: value}) for value in ([], [row])]
    events = run(engine, calls, 'Please handle the requested figures, not 2 percent.')
    assert len(events) == 1 and events[0]['type'] == 'ask', events
    text = events[0]['text'].lower()
    assert all(part in text for part in expected), events
    assert not any(part in text for part in ('not specified', '{', '}', '[', ']', 'bank_lines')), events
