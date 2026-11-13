"""Written coordinated bank choices retain both names."""
import pytest
from sqlalchemy import update

from bookdiff import snapshot as all_books
from erpclaw_assistant.model import Reply, call
from erpclaw_assistant.resolve_payment import account_phrases, outgoing_account_phrases
from erpclaw_assistant.resolve_words import _as_written
from erpclaw_core import models as m
from test_outgoing_account import books, payment, rename_bank, snapshot
from test_payment_phrase_misc import receipts
from test_payment_resolve import _add_bank, kinds, lines, pay, turns
from test_reversal import pay as post_payment


@pytest.mark.parametrize('join', ['or', 'and', 'either'])
@pytest.mark.parametrize('outgoing', [False, True])
def test_coordinated_readers_keep_written_names(join, outgoing):
    prefix = 'either ' if join == 'either' else ''
    conjunction = 'or' if join == 'either' else join
    text = f"{'from' if outgoing else 'into'} {prefix}Checking {conjunction} May Savings"
    if outgoing:
        found = outgoing_account_phrases(text, [{'name': 'Checking', 'number': '1100'}, {'name': 'May Savings', 'number': '1110'}])
        spans = [(f.start, f.end) for f in found]
    else:
        spans = account_phrases(text)
    assert [_as_written(text, span) for span in spans] == ['Checking', 'May Savings']


@pytest.mark.parametrize('choice,options', [
    ('Bank or May Savings', ['Bank', 'May Savings']),
    ('May Savings or Bank', ['May Savings', 'Bank']),
    ('Bank and May Savings', ['Bank', 'May Savings']),
    ('either Bank or May Savings', ['Bank', 'May Savings']),
])
def test_real_receipt_choice_asks(receipts, choice, options):
    _add_bank(receipts, '1110', 'May Savings')
    before = all_books(receipts)
    events = pay(receipts, 'Hooli paid 100 into ' + choice, customer='Hooli', amount=100)
    assert kinds(events) == ['ask'], events
    assert events[0]['options'] == options
    assert all_books(receipts) == before


@pytest.mark.parametrize('choice,options', [
    ('Checking or May Savings', ['Checking', 'May Savings']),
    ('May Savings or Checking', ['May Savings', 'Checking']),
    ('Checking and May Savings', ['Checking', 'May Savings']),
    ('either Checking or May Savings', ['Checking', 'May Savings']),
])
def test_outgoing_choice_asks(books, choice, options):
    rename_bank(books, 'Checking')
    with books[0].begin() as conn:
        conn.execute(update(m.account).where(m.account.c.number == '1110').values(name='May Savings'))
    before = all_books(books[0])
    out = payment(books, 'pay Copperline 500 from ' + choice)
    assert out.kinds == ['ask'], out.events
    assert out.events[0]['options'] == options
    assert all_books(books[0]) == before

def test_record_payment_comma_bank_cards(receipts):
    _add_bank(receipts, '1110', 'Savings')
    before = snapshot(receipts, cards=False)
    events = pay(receipts, 'Hooli paid 100, Savings', customer='Hooli', amount=100)
    assert kinds(events) == ['card'], events
    assert lines(events[0])['Account'] == ['1110 Savings']
    assert snapshot(receipts, cards=False) == before


def test_reverse_payment_comma_bank_asks(engine):
    payment_row = post_payment(engine, '100')
    _add_bank(engine, '1110', 'Savings')
    before = all_books(engine)
    events = turns(engine, Reply('', [call('reverse_payment', payment=payment_row['number'])])).turn(
        'reverse', f"reverse payment {payment_row['number']}, Savings")
    assert kinds(events) == ['ask'] and events[0]['code'] == 'account_ambiguous', events
    assert 'Savings' in events[0]['text']
    assert all_books(engine) == before


def test_cancel_invoice_comma_bank_asks(engine):
    _add_bank(engine, '1110', 'Savings')
    before = all_books(engine)
    events = turns(engine, Reply('', [call('cancel_invoice', invoice='INV-2026-00003')])).turn(
        'cancel', 'cancel invoice INV-2026-00003, Savings')
    assert kinds(events) == ['ask'] and events[0]['code'] == 'account_ambiguous', events
    assert 'Savings' in events[0]['text']
    assert all_books(engine) == before
