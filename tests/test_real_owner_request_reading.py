import pytest

from card_reach_support import assistant, invoice, card


def test_supplier_absent_contact_reaches_card(engine):
    a = assistant(engine, 'add_supplier', name='Frost Gear Workshop')
    card(a.turn('supplier', "hey can you add Frost Gear Workshop as a new supplier? i don't have an email or city for them"))


@pytest.mark.parametrize('name,unit,price,owner_text', [
    ('Elm Packing Service', 'each', '6.40', 'hey need to add a new item - Elm Packing Service, its a service so no stock or warehouse, we sell it per each for 6.40'),
    ('Frost Gauge Calibration', 'each', '35.00', "Hi, need to set up a new item - Frost Gauge Calibration. It's a service, we sell it per each for $35.00. No stock or warehouse needed for this one."),
    ('Juniper Fitting Advice', 'hour', '19.80', 'hey it said busy again - can you try one more time for me? need that Juniper Fitting Advice set up as a service, billed per hour at 19.80 USD, no stock or warehouse needed'),
])
def test_service_request_reaches_card_after_kind_choice(engine, name, unit, price, owner_text):
    a = assistant(engine, 'add_item', name=name, unit=unit, price=price)
    result = a.turn(name, owner_text)
    if result[-1]['type'] == 'ask':
        assert result[-1]['code'] == 'item_kind_missing', result
        result = a.turn(name, '1 - a service I sell')
    c = card(result)
    assert name in str(c) and price in str(c), c


def test_invoice_post_is_one_change(engine):
    a = invoice(engine, 'Hooli', [{'item': 'Copper Wire', 'quantity': 4}])
    card(a.turn('invoice-post', 'hi need an invoice for Hooli - 4 units of Copper Wire at 28 bucks each. just post it dont email or make new customer/item use whats there'))


def test_draft_invoice_quantity_before_unit(engine):
    a = invoice(engine, 'Pied Piper', [{'item': 'Circuit Board', 'quantity': 4}])
    card(a.turn('invoice-draft', "hey can you do a draft invoice for Pied Piper for 4 Circuit Boards at $45 each? just save it as draft, don't email it"))


@pytest.mark.parametrize('suffix', [
    "i don't have an email, just skip it and add Frost Gear Workshop without it",
    "i already said i don't have one, just leave email blank and add Frost Gear Workshop",
    'ugh ive told you 3 times now i got no email - just skip it and add Frost Gear Workshop',
])
def test_absent_email_never_creates_a_field(engine, suffix):
    a = assistant(engine, 'add_supplier', name='Frost Gear Workshop')
    result = a.turn('absent', suffix)
    if '3 times' in suffix:
        assert result[-1]['code'] == 'figure_ambiguous', result
    else:
        card(result)


def test_unplaced_number_still_asks(engine):
    a = assistant(engine, 'add_item', name='Elm Packing Service', unit='each', price='6.40')
    result = a.turn('unplaced', 'add Elm Packing Service as a service per each for 6.40 and 7')
    assert result[-1]['code'] == 'figure_ambiguous', result
    assert '7' in result[-1]['text'], result


def test_numbered_choice_does_not_become_price(engine):
    a = assistant(engine, 'add_item', name='Elm Packing Service', unit='each', price='6.40')
    q = a.turn('choice', 'add Elm Packing Service as a service at 6.40 per each')
    assert q[-1]['code'] == 'item_kind_missing', q
    c = card(a.turn('choice', '1 - a service I sell'))
    assert '6.40' in str(c), c


def test_second_distinct_action_still_asks(engine):
    a = invoice(engine, 'Hooli', [{'item': 'Copper Wire', 'quantity': 4}])
    result = a.turn('two', 'invoice Hooli for 4 Copper Wire and create a customer')
    assert result[-1]['code'] == 'changes_ambiguous', result


@pytest.mark.parametrize('text', [
    'Invoice the one website audit',
    'Bill Hooli for one website audit',
    'Invoice that two website audit',
    'Invoice the one website audit at 25% tax',
])
def test_quantity_word_before_item_after_determiner(monkeypatch, text):
    from erpclaw_assistant import resolve_inventory as ri

    monkeypatch.setattr(ri, 'customers', lambda ctx: [])
    occurrences = ri.inventory(None, text, [{'name': 'Website audit'}])
    quantities = [o.value for o in occurrences if o.kind == 'number']
    assert quantities == ([2] if 'two' in text else [1])
    if '25%' in text:
        assert all(o.value != 25 for o in occurrences if o.kind == 'number')


def test_this_one_without_item_is_not_a_figure(monkeypatch):
    from erpclaw_assistant import resolve_inventory as ri

    monkeypatch.setattr(ri, 'customers', lambda ctx: [])
    occurrences = ri.inventory(None, 'set up this one as a service', [])
    assert not [o for o in occurrences if o.kind in ('figure', 'number')]


@pytest.mark.parametrize('owner_text', [
    'add the supplier; email, leave it blank',
    'add the supplier; email: none',
    'add the supplier; no email for them',
    'add the supplier; email - skip it',
    'add the supplier; email, leave it out',
    'add the supplier; email, without it',
    "add the supplier; email, don't have it",
    'add the supplier; skip it, email',
    'add the supplier; without email',
])
def test_omitted_email_word_orders(engine, owner_text):
    a = assistant(engine, 'add_supplier', name='Frost Gear Workshop')
    card(a.turn('supplier-omitted', 'Frost Gear Workshop: ' + owner_text))


def test_email_address_is_placed(engine):
    address = 'ap' + '@' + 'frost.example'
    a = assistant(engine, 'add_supplier', name='Frost Gear Workshop', email=address)
    c = card(a.turn('supplier-email', 'add Frost Gear Workshop; email ' + address))
    assert address in str(c)
