"""Approval rejects cards made before the payment operations were released."""
from dataclasses import replace
import pytest
from sqlalchemy import update
from erpclaw_core import approvals, models as m
from erpclaw_core.common import Refusal
from erpclaw_core.plugins import REGISTRY


@pytest.mark.parametrize('name', ['record_payment', 'reverse_payment', 'cancel_invoice'])
def test_enabled_copy_refused(engine, monkeypatch, name):
    contract = REGISTRY.contracts[name]
    enabled = replace(contract, planned=None)
    monkeypatch.setattr(contract, 'planned', 'Core')
    before = book_rows(engine)
    with engine.connect() as conn:
        cards = conn.execute(select(m.approval)).all()
    with pytest.raises(Refusal) as exc:
        approvals.propose(engine, enabled, {})
    assert exc.value.code == 'planned'
    assert book_rows(engine) == before
    with engine.connect() as conn:
        assert conn.execute(select(m.approval)).all() == cards


def test_unregistered_contract_is_refused_before_a_card(engine):
    contract = replace(REGISTRY.contracts['record_payment'], name='unregistered_payment')
    before = all_books(engine)
    with pytest.raises(Refusal) as exc:
        approvals.propose(engine, contract, {})
    assert exc.value.code == 'not_a_write'
    assert str(exc.value) == 'unregistered_payment is not a write with a declared read set.'
    assert all_books(engine) == before


def test_unparseable_version_superseded(engine):
    c = approvals.propose(engine, REGISTRY.contracts['record_payment'], {'customer': 'Hooli', 'amount': '100'})
    with engine.begin() as conn:
        conn.execute(update(m.approval).where(m.approval.c.id == c.approval_id).values(contract_version='invalid'))
    out = approvals.approve(engine, c.approval_id, c.digest)
    assert out.status == 'superseded'

from sqlalchemy import select
from erpclaw_sales import tables as t
from test_reversal import invoice, pay
from bookdiff import snapshot as all_books, changes as books_changes


def book_rows(engine):
    tables = (m.posting, m.ledger_entry, m.number_series, m.operation_outcome, t.payment, t.allocation, t.invoice)
    with engine.connect() as conn:
        return [sorted(map(repr, conn.execute(select(table)).all())) for table in tables]


@pytest.mark.parametrize('name', ['record_payment', 'reverse_payment', 'cancel_invoice'])
@pytest.mark.parametrize('old_version', ['0.0.2', 'not-a-version'])
def test_old_cards_are_superseded_without_book_changes(engine, name, old_version):
    if name == 'record_payment':
        args = {'customer': 'Hooli', 'amount': '100'}
    elif name == 'reverse_payment':
        args = {'payment': pay(engine, '100')['number']}
    else:
        args = {'invoice': invoice(engine)['number']}
    card = approvals.propose(engine, REGISTRY.contracts[name], args)
    with engine.begin() as conn:
        conn.execute(update(m.approval).where(m.approval.c.id == card.approval_id).values(contract_version=old_version))
    before = all_books(engine)
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == 'superseded' and result.card is None
    assert result.data['code'] == 'planned'
    assert 'not in ERPClaw yet' in result.text
    changed = books_changes(before, all_books(engine))
    assert set(changed) == {'approval'}
    assert not changed['approval']['added'] and not changed['approval']['removed']
    [(old, new)] = changed['approval']['changed']
    assert old['id'] == new['id'] == card.approval_id
    assert {key for key in old if old[key] != new[key]} <= {'status', 'decision_json', 'decided_at'}


def test_card_made_while_on_ends_when_off(engine, monkeypatch):
    card = approvals.propose(engine, REGISTRY.contracts['record_payment'], {'customer': 'Hooli', 'amount': '100'})
    monkeypatch.setattr(REGISTRY.contracts['record_payment'], 'planned', 'Core')
    before = book_rows(engine)
    result = approvals.approve(engine, card.approval_id, card.digest)
    assert result.status == 'superseded' and result.data['code'] == 'planned'
    assert book_rows(engine) == before


def test_current_card_commits(engine):
    card = approvals.propose(engine, REGISTRY.contracts['record_payment'], {'customer': 'Hooli', 'amount': '100'})
    assert approvals.approve(engine, card.approval_id, card.digest).status == 'committed'


def test_released_tools_include_the_three_payment_operations(engine, as_released):
    from erpclaw_assistant.graph import Assistant
    from erpclaw_assistant.model import Reply, ScriptedModel
    model = ScriptedModel([Reply('Hello.')])
    Assistant(engine, model).turn('offered-payment-tools', 'hello')
    assert set(model.tools_seen[0]) == {
        'set_up_company', 'add_customer', 'add_supplier', 'add_cash_account', 'add_item', 'list_items',
        'list_customers', 'list_suppliers',
        'create_invoice', 'submit_invoice', 'record_payment', 'cancel_invoice', 'reverse_payment',
        'find_capability', 'choose_capability',
    }
