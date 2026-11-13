from card_reach_support import invoice, card


def test_invoice_two_known_items_reaches_card(engine):
    a = invoice(engine, 'Pied Piper', [{'item': 'Copper Wire', 'quantity': 5},
                                       {'item': 'Circuit Board', 'quantity': 2}])
    result = a.turn('two', 'invoice Pied Piper for 5 Copper Wire and 2 Circuit Board')
    c = card(result)
    assert 'Copper Wire' in str(c) and 'Circuit Board' in str(c), c


def test_consumed_customer_and_item_are_not_asked_about(engine):
    a = invoice(engine, 'Hooli', [{'item': 'Motor Housing Unit', 'quantity': 1}])
    card(a.turn('named', 'invoice Hooli for 1 Motor Housing Unit'))


def test_price_phrase_belongs_to_its_line(engine):
    a = invoice(engine, 'Hooli', [{'item': 'Motor Housing Unit', 'quantity': 1}])
    result = a.turn('price', 'invoice Hooli for 1 Motor Housing Unit at $35.00')
    c = card(result)
    assert '@ 35.00 = 35.00' in str(c['card']['lines']), c
