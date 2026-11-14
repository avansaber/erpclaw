"""A purpose is shown on a card without becoming a payment field."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from bookdiff import snapshot as all_books
from erpclaw_assistant.model import Reply, call
from ownership_helpers import owner_check
from erpclaw_core import approvals, ledger, models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_sales import service as sales, tables as t
from test_payment_amounts import client, say
from test_payment_phrase_misc import receipts
from test_payment_resolve import _add_bank, kinds, lines, pay, turns
from test_reversal import invoice, pay as post_payment


def test_for_sales_keeps_card(receipts):
    events = pay(receipts, 'Hooli paid 100 for sales', customer='Hooli', amount=100)
    assert kinds(events) == ['card'], events
    assert ['Purpose (not saved)', 'for sales'] in events[0]['card']['lines']


@pytest.mark.parametrize('purpose', ['for sales', 'for the sales'])
def test_purpose_is_displayed_without_being_stored(receipts, purpose):
    [event] = pay(receipts, 'Hooli paid 100 ' + purpose, customer='Hooli', amount=100)
    assert event['type'] == 'card', event
    assert ['Purpose (not saved)', purpose] in event['card']['lines']
    with receipts.connect() as conn:
        stored = conn.execute(select(m.approval.c.card_json).where(m.approval.c.id == event['approval_id'])).scalar_one()
    assert purpose not in stored


def test_replacement_card_and_replay_recover_purpose(engine):
    inv = invoice(engine)
    c, cid = client(engine, Reply('', [call('record_payment', customer='Hooli', amount=100)]))
    [event] = say(c, cid, 'Hooli paid 100 for sales')
    assert ['Purpose (not saved)', 'for sales'] in event['card']['lines']
    post_payment(engine, '20', number=inv['number'])
    fresh = approvals.approve(engine, event['approval_id'], event['digest'], ownership_check=owner_check(engine))
    assert fresh.status == 'superseded' and fresh.card is not None
    assert ['Purpose (not saved)', 'for sales'] in fresh.card['lines']
    replay = approvals.approve(engine, event['approval_id'], event['digest'], ownership_check=owner_check(engine))
    assert replay.card == fresh.card
    with engine.connect() as conn:
        stored = conn.execute(select(m.approval.c.card_json).where(m.approval.c.id == fresh.approval_id)).scalar_one()
        assert 'for sales' not in stored
    result = approvals.approve(engine, fresh.approval_id, fresh.digest, ownership_check=owner_check(engine))
    assert result.status == 'committed'
    with engine.connect() as conn:
        payment = conn.execute(select(t.payment).where(t.payment.c.operation_key == fresh.operation_key)).mappings().one()
        assert not payment['reference']
    assert 'for sales' not in str(all_books(engine)).lower()

@pytest.fixture
def purpose_books(receipts):
    with receipts.begin() as conn:
        ctx = company_ctx(conn)
        sales_account = ledger.account_by_number(ctx, '4100')
        assert sales_account['name'] == 'Sales' and sales_account['account_type'] != 'bank'
        for number, name, root, account_type in [('4191', 'Sales Revenue', 'income', 'income'),
                                                  ('1192', 'Petty Cash', 'asset', None)]:
            parent = ledger.account_by_number(ctx, '4000' if root == 'income' else '1000')['id']
            conn.execute(m.account.insert().values(id=new_id(), company_id=ctx.company_id, parent_id=parent,
                         number=number, name=name, root_type=root, account_type=account_type, is_group=False))
    return receipts


def test_income_account_is_displayed_as_purpose(purpose_books):
    [event] = pay(purpose_books, 'Hooli paid 100 for Sales Revenue', customer='Hooli', amount=100)
    assert event['type'] == 'card', event
    assert ['Purpose (not saved)', 'for Sales Revenue'] in event['card']['lines']
    assert lines(event)['Account'] == ['1100 Bank']


def test_non_bank_destination_still_refused(purpose_books):
    before = all_books(purpose_books)
    events = pay(purpose_books, 'Hooli paid 100 into Petty Cash', customer='Hooli', amount=100)
    assert kinds(events) == ['refused'] and events[0]['code'] == 'account_not_bank', events
    assert all_books(purpose_books) == before


@pytest.mark.parametrize('purpose', ['for sales', 'for the sales', 'for Sales Revenue'])
def test_purpose_with_two_banks_asks_then_displays(purpose_books, purpose):
    _add_bank(purpose_books, '1110', 'Savings')
    assistant = turns(purpose_books, Reply('', [call('record_payment', customer='Hooli', amount=100)]),
                      Reply('', [call('record_payment', customer='Hooli', amount=100, account='Savings')]))
    before = all_books(purpose_books)
    events = assistant.turn('purpose', 'Hooli paid 100 ' + purpose)
    assert kinds(events) == ['ask'] and events[0]['code'] == 'account_ambiguous', events
    assert events[0]['options'] == ['1100 Bank', '1110 Savings']
    assert all_books(purpose_books) == before
    [event] = assistant.turn('purpose', '1110 Savings')
    assert event['type'] == 'card', event
    assert ['Purpose (not saved)', purpose] in event['card']['lines']
    with purpose_books.begin() as conn:
        ctx = company_ctx(conn)
        customer = dict(conn.execute(select(m.party).where(m.party.c.name == 'Hooli')).mappings().one())
        sales.record_payment(ctx, sales.plan_payment(ctx, customer, Decimal('20'), ctx.today, account='Bank'))
    fresh = approvals.approve(purpose_books, event['approval_id'], event['digest'], ownership_check=owner_check(purpose_books))
    assert fresh.status == 'superseded' and ['Purpose (not saved)', purpose] in fresh.card['lines']
    assert approvals.approve(purpose_books, event['approval_id'], event['digest'], ownership_check=owner_check(purpose_books)).card == fresh.card


def test_purpose_ignores_model_account(purpose_books):
    [event] = pay(purpose_books, 'Hooli paid 100 for sales', customer='Hooli', amount=100,
                  account='Petty Cash')
    assert event['type'] == 'card', event
    assert lines(event)['Account'] == ['1100 Bank']
    assert ['Purpose (not saved)', 'for sales'] in event['card']['lines']


@pytest.mark.parametrize('name,number,is_group,frozen,code', [
    ('Frozen Bank', '1193', False, True, 'account_frozen'),
    ('Group Bank', '1194', True, False, 'account_not_bank'),
])
def test_unusable_destination_still_refused(purpose_books, name, number, is_group, frozen, code):
    with purpose_books.begin() as conn:
        ctx = company_ctx(conn)
        conn.execute(m.account.insert().values(id=new_id(), company_id=ctx.company_id,
                     parent_id=ledger.account_by_number(ctx, '1000')['id'], number=number, name=name,
                     root_type='asset', account_type='bank', is_group=is_group, frozen=frozen))
    before = all_books(purpose_books)
    events = pay(purpose_books, f'Hooli paid 100 into {name}', customer='Hooli', amount=100)
    assert kinds(events) == ['refused'] and events[0]['code'] == code, events
    assert all_books(purpose_books) == before


def test_purpose_with_two_banks_ignores_model_account(purpose_books):
    _add_bank(purpose_books, '1110', 'Savings')
    before = all_books(purpose_books)
    events = pay(purpose_books, 'Hooli paid 100 for sales', customer='Hooli', amount=100, account='Savings')
    assert kinds(events) == ['ask'] and events[0]['code'] == 'account_ambiguous', events
    assert events[0]['options'] == ['1100 Bank', '1110 Savings']
    assert all_books(purpose_books) == before
