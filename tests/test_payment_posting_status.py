"""Payment result status is measured at its posting sequence."""
from sqlalchemy import select
from erpclaw_core import approvals, models as m
from erpclaw_sales import tables as t
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import _payment_text
from test_reversal import invoice, pay


def books_snapshot(engine):
    with engine.connect() as conn:
        return [sorted(map(repr, conn.execute(select(table)).all()))
                for table in (t.payment, t.allocation, t.invoice, m.posting, m.ledger_entry)]


def test_reversal_before_payment_is_excluded(engine):
    inv = invoice(engine)
    first = pay(engine, '100')
    card = approvals.propose(engine, REGISTRY.contracts['reverse_payment'], {'payment': first['number']})
    approvals.approve(engine, card.approval_id, card.digest)
    second = pay(engine, '180')
    before = books_snapshot(engine)
    with engine.connect() as conn:
        result = _payment_text(company_ctx(conn), second)
    assert books_snapshot(engine) == before
    assert f"{inv['number']} 180.00 USD (now partly paid)" in result.text
    pay(engine, '100')
    with engine.connect() as conn:
        later_readback = _payment_text(company_ctx(conn), second)
    assert later_readback.text == result.text


def test_credit_before_payment_is_counted(engine):
    inv = invoice(engine)
    credit = approvals.propose(engine, REGISTRY.contracts['create_credit_note'],
                               {'invoice': inv['number'], 'items': [{'item': 'Copper Wire', 'quantity': '2'}]})
    assert approvals.approve(engine, credit.approval_id, credit.digest).status == 'committed'
    second = pay(engine, '224')
    before = books_snapshot(engine)
    with engine.connect() as conn:
        result = _payment_text(company_ctx(conn), second)
    assert books_snapshot(engine) == before
    assert f"{inv['number']} 224.00 USD (now paid)" in result.text


def test_tiny_remainder_agrees_on_card_and_commit(engine):
    from decimal import Decimal
    inv = invoice(engine)
    pay(engine, '279.99', number=inv['number'])
    card = approvals.propose(engine, REGISTRY.contracts['record_payment'],
                             {'customer': 'Hooli', 'amount': '0.01', 'invoice': inv['number']})
    assert 'now paid' in str(card.card['lines'])
    outcome = approvals.approve(engine, card.approval_id, card.digest)
    assert outcome.status == 'committed' and '(now paid)' in outcome.text
    with engine.connect() as conn:
        row = conn.execute(select(t.invoice).where(t.invoice.c.id == inv['id'])).mappings().one()
    assert Decimal(row['outstanding']) == 0 and row['status'] == 'paid'


def test_negative_receipt_refused_without_allocations(engine):
    import pytest
    from erpclaw_core.common import Refusal
    from erpclaw_sales.service import payment_amount
    before = books_snapshot(engine)
    with engine.connect() as conn, pytest.raises(Refusal) as refused:
        payment_amount(company_ctx(conn), '-0.01')
    assert refused.value.code == 'bad_amount'
    assert books_snapshot(engine) == before


def test_negative_allocation_is_rejected_by_books(engine):
    import pytest
    from decimal import Decimal
    from erpclaw_core.common import new_id
    from sqlalchemy.exc import IntegrityError
    inv = invoice(engine)
    other = invoice(engine)
    payment = pay(engine, '100', number=inv['number'])
    before = books_snapshot(engine)
    with pytest.raises(IntegrityError) as refused:
        with engine.begin() as conn:
            conn.execute(t.allocation.insert().values(id=new_id(), company_id=inv['company_id'],
                         payment_id=payment['id'], invoice_id=other['id'], currency=other['currency'], amount=Decimal('-0.01')))
    assert refused.value.orig.diag.constraint_name == 'ck_sales_payment_allocation_amount_sign'
    assert books_snapshot(engine) == before


def test_multiple_invoices_are_capped_and_excess_is_credit(engine):
    from decimal import Decimal
    first = invoice(engine)
    second = invoice(engine, qty='5')
    card = approvals.propose(engine, REGISTRY.contracts['record_payment'],
                             {'customer': 'Hooli', 'amount': '500', 'oldest_first': True})
    assert '80.00 USD stays as credit' in str(card.card['warnings'])
    assert approvals.approve(engine, card.approval_id, card.digest).status == 'committed'
    with engine.connect() as conn:
        payment = conn.execute(select(t.payment).where(t.payment.c.operation_key == card.operation_key)).mappings().one()
        amounts = dict(conn.execute(select(t.allocation.c.invoice_id, t.allocation.c.amount).where(
            t.allocation.c.payment_id == payment['id'])).all())
    assert amounts == {first['id']: Decimal('280.00'), second['id']: Decimal('140.00')}
    assert Decimal(payment['unallocated']) == Decimal('80.00')


def test_earlier_card_cannot_post_after_another_card_pays(engine):
    inv = invoice(engine)
    args = {'customer': 'Hooli', 'amount': '280', 'invoice': inv['number']}
    earlier = approvals.propose(engine, REGISTRY.contracts['record_payment'], args)
    later = approvals.propose(engine, REGISTRY.contracts['record_payment'], args)
    assert approvals.approve(engine, later.approval_id, later.digest).status == 'committed'
    before = books_snapshot(engine)
    result = approvals.approve(engine, earlier.approval_id, earlier.digest)
    assert result.status == 'superseded'
    assert books_snapshot(engine) == before
