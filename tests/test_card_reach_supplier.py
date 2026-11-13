from card_reach_support import assistant, card


def test_supplier_without_email_reaches_card(engine):
    a = assistant(engine, 'add_supplier', name='Acme Parts')
    c = card(a.turn('supplier', 'Add a supplier called Acme Parts'))
    assert c['card']['title'] == 'Add a supplier', c


def test_model_email_is_not_invented_for_supplier(engine):
    a = assistant(engine, 'add_supplier', name='Acme Parts', email='invented@example.com')
    c = card(a.turn('supplier-no-email', 'Add a supplier called Acme Parts'))
    assert 'invented@example.com' not in str(c), c


def test_email_in_supplier_name_is_not_an_email_field(engine):
    a = assistant(engine, 'add_supplier', name='Email Parts')
    c = card(a.turn('supplier-name', 'Add a supplier called Email Parts'))
    assert 'Email Parts' in str(c), c
