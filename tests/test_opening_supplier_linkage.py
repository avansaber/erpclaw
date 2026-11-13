"""Opening supplier items retain their actual posting and settle with delivery debts."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bookdiff import changes, snapshot
from erpclaw_core import approvals, ledger, masters, models as m, supplier_payments as payments
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.payable_tables import supplier_payable as items
from erpclaw_core.plugins import REGISTRY


@pytest.fixture(autouse=True)
def activated(every_feature):
    pass


def setup(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        suppliers = [masters.create_party(ctx, 'supplier', name) for name in ('Supplier X', 'Supplier Y')]
        bank = ledger.account_by_type(ctx, 'bank')['id']
        payable = ledger.account_by_type(ctx, 'payable')['id']
        equity = ledger.account_by_type(ctx, 'opening_balance')['id']
        start = conn.execute(select(m.fiscal_year.c.start_date).where(
            m.fiscal_year.c.company_id == ctx.company_id).order_by(m.fiscal_year.c.start_date).limit(1)).scalar_one()
    return suppliers, bank, payable, equity, start


def propose(engine, bank='5000.00', amounts=('1200.00', '400.00')):
    return approvals.propose(engine, REGISTRY.contracts['record_opening_position'], {
        'bank_lines': [{'account': 'Bank', 'amount': bank}] if bank else [],
        'supplier_lines': [{'supplier': name, 'amount': amount, 'old_reference': ref,
                            'due_date': '2026-02-01'} for name, amount, ref in
                           zip(('Supplier X', 'Supplier Y'), amounts, ('Bill 88', 'Dec statement'))]})


def committed(engine, card):
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == 'committed', out.text
    return out


def rows(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(items).where(items.c.source_type == 'opening')
                .order_by(items.c.opening_sequence)).mappings()]


def test_opening_supplier_items_link_to_the_single_opening_posting_and_its_control_lines(engine):
    suppliers, bank, payable, equity, start = setup(engine)
    card = propose(engine)
    before = snapshot(engine)
    out = committed(engine, card)
    after = snapshot(engine)
    added = changes(before, after)['supplier_payable']
    assert len(added['added']) == 2 and not added['changed'] and not added['removed']
    stored = rows(engine)
    assert len({r['source_id'] for r in stored}) == 2
    assert [r['supplier_id'] for r in stored] == suppliers
    assert [r['opening_sequence'] for r in stored] == [1, 2]
    assert [r['old_reference'] for r in stored] == ['Bill 88', 'Dec statement']
    assert all(r['due_date'] == date(2026, 2, 1) for r in stored)
    with engine.connect() as conn:
        posting = conn.execute(select(m.posting).where(m.posting.c.id == out.data['posting_id'])).mappings().one()
        entries = conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.posting_id == posting['id'])
                               .order_by(m.ledger_entry.c.line_no)).mappings().all()
        assert len(entries) == 4
        assert posting['posting_date'] == start and posting['voucher_type'] == 'opening'
        assert all(r['posting_id'] == posting['id'] and r['source_voucher_id'] == posting['voucher_id']
                   and r['source_number'] == out.data['number'] == posting['memo'] for r in stored)
        for item in stored:
            [line] = [e for e in entries if e['line_no'] == item['control_line_no']]
            assert (line['account_id'], line['party_id'], line['credit'], line['debit']) == (
                payable, item['supplier_id'], item['amount'], 0)
        assert ledger.verify_chain(conn, posting['company_id']) is None
    assert 'Bill 88' in str(card.card) and '2026-02-01' in str(card.card)


@pytest.mark.parametrize('voucher', ['opening', 'journal'])
def test_direct_supplier_party_posting_is_refused(engine, voucher):
    suppliers, bank, payable, _, start = setup(engine)
    before = snapshot(engine)
    with engine.begin() as conn:
        with pytest.raises(Refusal, match='supplier') as err:
            ledger.post(company_ctx(conn), start, voucher, new_id(), [
                ledger.Line(bank, debit=Decimal('100')), ledger.Line(payable, credit=Decimal('100'), party_id=suppliers[0])])
        assert err.value.code == 'payable_source_not_allowed'
    assert changes(before, snapshot(engine)) == {}


@pytest.mark.parametrize('bank,expected,side', [('500.00', '1100.00', 'debit'), ('1600.00', None, None), (None, '1600.00', 'debit')])
def test_negative_and_zero_equity(engine, bank, expected, side):
    _, _, _, equity, _ = setup(engine)
    card = propose(engine, bank)
    out = committed(engine, card)
    with engine.connect() as conn:
        lines = conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.posting_id == out.data['posting_id'])).mappings().all()
    found = [r for r in lines if r['account_id'] == equity]
    assert sum(r['debit'] for r in lines) == sum(r['credit'] for r in lines)
    if expected:
        assert len(found) == 1 and found[0][side] == Decimal(expected)
        assert 'owes more than it has' in str(card.card)
    else:
        assert not found
        assert '0.00' in dict(card.card['lines'])["Opening balance (owner's equity)"]
    assert 'what the business owns minus what it owes, at the start' in str(card.card)
    assert 'You or your accountant can split this into capital and retained earnings later.' in str(card.card)
    assert 'what the business is worth' not in str(card.card)


def test_duplicate_supplier_in_opening_is_refused(engine):
    setup(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        approvals.propose(engine, REGISTRY.contracts['record_opening_position'], {'supplier_lines': [
            {'supplier': 'Supplier X', 'amount': '100.00'}, {'supplier': 'Supplier X', 'amount': '200.00'}]})
    assert err.value.code == 'duplicated_part'
    assert changes(before, snapshot(engine)) == {}


@pytest.mark.parametrize('amount', ['0.00', '-1.00', '1.001', 'NaN', 'USD 10'])
def test_invalid_supplier_amount_refuses_without_writes(engine, amount):
    setup(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as err:
        propose(engine, amounts=(amount,))
    assert err.value.code == 'bad_amount'
    assert changes(before, snapshot(engine)) == {}


def test_opening_supplier_items_are_settleable_through_verified_allocations(engine):
    suppliers, bank, _, _, start = setup(engine)
    committed(engine, propose(engine))
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        plan = payments.plan_payment(ctx, payments.PaySupplier(suppliers[0], bank, '500.00', start))
        assert [(a.item.source_id, a.amount, a.outstanding_after) for a in plan.allocations] == [
            (rows(engine)[0]['source_id'], Decimal('500.00'), Decimal('700.00'))]
        assert ('posting', rows(engine)[0]['posting_id']) in plan.read_set


@pytest.mark.parametrize('field,value', [('posting_id', 'missing'), ('source_voucher_id', 'missing'),
    ('control_line_no', 1), ('source_number', 'wrong'), ('posting_date', date(2026, 1, 2)),
    ('supplier_id', 'wrong'), ('amount', Decimal('1.00')), ('currency', 'EUR')])
def test_opening_source_verification_uses_persisted_contributions(engine, field, value):
    suppliers, _, payable, _, _ = setup(engine)
    committed(engine, propose(engine))
    item = {**rows(engine)[0], field: value}
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        with pytest.raises(Refusal):
            payments._source(ctx, item, suppliers[0], {'id': payable}, set(), payments.MAX_HISTORY, {}, set())


def test_unique_source_and_insert_only_linkage_are_preserved(engine):
    setup(engine)
    committed(engine, propose(engine))
    [item, _] = rows(engine)
    assert [c.name for c in next(c for c in items.constraints if c.name == 'uq_supplier_payable_source').columns] == [
        'company_id', 'source_type', 'source_id']
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(items.insert().values({**item, 'id': new_id()}))
    for field, value in [('control_line_no', 1), ('opening_sequence', 9), ('old_reference', 'changed')]:
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(items.update().where(items.c.id == item['id']).values({field: value}))


@pytest.fixture
def payment_contract(monkeypatch):
    """Exercise the internal writer through real approval without releasing a new operation."""
    from erpclaw_core import approval_scope
    from erpclaw_core.contracts import Input, WriteContract

    class PaymentInput(Input):
        supplier_id: str
        bank_account_id: str
        amount: str
        date: date

        @property
        def posting_date(self):
            return self.date

    contract = WriteContract('pay_supplier', 'core', 'write', 'Pay a supplier', PaymentInput,
        preview=payments.preview_payment, read_set=payments.payment_read_set, commit=payments.commit_payment,
        find_by_operation_key=payments.find_payment_by_key, moves_money=True, amounts=('amount', 'outstanding'))
    for callback in ('preview', 'read_set', 'commit', 'find_by_operation_key'):
        setattr(contract, callback, approval_scope._wrap_contract(getattr(contract, callback), 'core',
                                                               'pay_supplier', callback))
    monkeypatch.setitem(REGISTRY.contracts, 'pay_supplier', contract)
    monkeypatch.setitem(REGISTRY.contract_plugin, 'pay_supplier', 'core')
    owner = REGISTRY.plugins['core']
    monkeypatch.setattr(owner, 'manifest', owner.manifest.model_copy(update={
        'contracts': {**owner.manifest.contracts, 'pay_supplier': 'write'}}))
    return contract


def pay(engine, contract, supplier, bank, amount, when):
    card = approvals.propose(engine, contract, dict(supplier_id=supplier, bank_account_id=bank,
                                                  amount=amount, date=when.isoformat()))
    return committed(engine, card)


def test_one_op_draw_before_chain_lock_for_each_opening_card(engine, monkeypatch):
    from erpclaw_core import opening_position as opening
    _, _, _, _, start = setup(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        fiscal_year = conn.execute(select(m.fiscal_year.c.label).where(
            m.fiscal_year.c.company_id == ctx.company_id, m.fiscal_year.c.start_date == start)).scalar_one()
        conn.execute(m.number_series.insert().values(company_id=ctx.company_id, series='OP',
                                                     fiscal_year=fiscal_year, next_value=7))
        series_before = {(r.company_id, r.series, r.fiscal_year): r.next_value
                         for r in conn.execute(select(m.number_series))}
    draws = []
    real_number, real_lock = opening.next_number, ledger.lock_chain

    def number(ctx, series, when):
        draws.append((series, when))
        return real_number(ctx, series, when)

    def lock(ctx):
        assert len(draws) == 1, 'the opening number must be drawn before the chain lock'
        return real_lock(ctx)

    monkeypatch.setattr(opening, 'next_number', number)
    monkeypatch.setattr(ledger, 'lock_chain', lock)
    # A bank-only card also owns a batch number.
    out = committed(engine, propose(engine, amounts=()))
    assert out.data['number'] == f'OP-{fiscal_year}-00007'
    assert draws == [('OP', start)]
    with engine.connect() as conn:
        series_after = {(r.company_id, r.series, r.fiscal_year): r.next_value
                        for r in conn.execute(select(m.number_series))}
        assert series_after == {**series_before, (ctx.company_id, 'OP', fiscal_year): 8}
        assert conn.execute(select(m.posting.c.memo).where(
            m.posting.c.operation_key == out.operation_key)).scalar_one() == out.data['number']
    before = snapshot(engine)
    assert approvals.read_back(engine, out.operation_key).data == out.data
    assert changes(before, snapshot(engine)) == {}


def test_refused_opening_rolls_back_number_posting_and_items(engine, monkeypatch):
    from erpclaw_core import payables
    setup(engine)
    card = propose(engine)
    real = payables.open_item

    def broken(ctx, **kwargs):
        real(ctx, **kwargs)
        raise Refusal('The supplier starting figure changed.', 'conflict')

    monkeypatch.setattr(payables, 'open_item', broken)
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == 'refused' and out.data['code'] == 'conflict'
    diff = changes(before, snapshot(engine))
    assert set(diff) == {'approval', 'operation_outcome'}
    after = snapshot(engine)
    replay = approvals.approve(engine, card.approval_id, card.digest)
    assert (replay.status, replay.data) == (out.status, out.data)
    assert changes(after, snapshot(engine)) == {}


def test_two_suppliers_in_one_batch_pay_and_reverse(engine, payment_contract):
    from erpclaw_core.payable_tables import supplier_payment_allocation as allocations
    suppliers, bank, _, _, start = setup(engine)
    opening = committed(engine, propose(engine))
    original = rows(engine)
    assert {r['source_number'] for r in original} == {opening.data['number']}
    first = pay(engine, payment_contract, suppliers[0], bank, '500.00', start)
    assert [r['outstanding'] for r in rows(engine)] == [Decimal('700.00'), Decimal('400.00')]
    pay(engine, payment_contract, suppliers[0], bank, '700.00', start)
    last = pay(engine, payment_contract, suppliers[1], bank, '400.00', start)
    assert [r['status'] for r in rows(engine)] == ['paid', 'paid']
    before = snapshot(engine)
    reversal = approvals.propose(engine, REGISTRY.contracts['reverse_supplier_payment'],
                                {'payment': last.data['number'], 'date': start.isoformat()})
    committed(engine, reversal)
    after = snapshot(engine)
    restored = rows(engine)
    assert [(r['status'], r['outstanding']) for r in restored] == [
        ('paid', Decimal('0')), ('open', Decimal('400.00'))]
    assert after['supplier_payment_allocation'] == before['supplier_payment_allocation']
    assert set(changes(before, after)['supplier_payable']) == {'added', 'removed', 'changed'}
    assert not changes(before, after)['supplier_payable']['added']
    assert not changes(before, after)['supplier_payable']['removed']
    for old, new in zip(original, restored):
        assert {k: v for k, v in old.items() if k not in ('status', 'outstanding')} == {
            k: v for k, v in new.items() if k not in ('status', 'outstanding')}
    with engine.connect() as conn:
        stored = conn.execute(select(allocations).where(allocations.c.payment_id == first.data['id'])).mappings().one()
        assert stored['payable_id'] == original[0]['id'] and stored['amount'] == Decimal('500.00')
        assert ledger.verify_chain(conn, original[0]['company_id']) is None
    before_replay = snapshot(engine)
    assert approvals.approve(engine, reversal.approval_id, reversal.digest).status == 'committed'
    assert changes(before_replay, snapshot(engine)) == {}


def test_delivery_and_opening_items_settle_oldest_first(engine, payment_contract):
    """The forward delivery card is not registered; seed its real core posting and payable rows."""
    from erpclaw_core.common import now
    suppliers, bank, payable, _, start = setup(engine)
    delivery_id, item_id = new_id(), new_id()
    later = date(2026, 1, 2)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        inventory = ledger.account_by_type(ctx, 'stock')['id']
        ledger.post(ctx, later, 'stock_goods_received', delivery_id, [
            ledger.Line(inventory, debit=Decimal('300.00')),
            ledger.Line(payable, credit=Decimal('300.00'), party_id=suppliers[0])])
        conn.execute(items.insert().values(id=item_id, company_id=ctx.company_id, supplier_id=suppliers[0],
            source_type='stock_goods_received', source_id=delivery_id, source_number='GR-2026-00001',
            posting_date=later, currency=ctx.currency, amount=Decimal('300.00'), outstanding=Decimal('300.00'),
            status='open', created_at=now()))
    committed(engine, propose(engine))
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        with pytest.raises(Refusal) as err:
            payments.plan_payment(ctx, payments.PaySupplier(suppliers[0], bank, '1300.00', start))
        assert err.value.code == 'payment_above_eligible'
        plan = payments.plan_payment(ctx, payments.PaySupplier(suppliers[0], bank, '1300.00', later))
        assert [(a.item.source_type, a.amount) for a in plan.allocations] == [
            ('opening', Decimal('1200.00')), ('stock_goods_received', Decimal('100.00'))]
    pay(engine, payment_contract, suppliers[0], bank, '1300.00', later)
    with engine.connect() as conn:
        row = conn.execute(select(items).where(items.c.id == item_id)).mappings().one()
        assert row['outstanding'] == Decimal('200.00')
    assert rows(engine)[0]['status'] == 'paid'


def test_only_opening_commit_opens_items_even_with_valid_linkage(engine, monkeypatch):
    from erpclaw_core import approval_scope, opening_position as opening, payables

    setup(engine)
    contract = REGISTRY.contracts[opening.NAME]
    card = propose(engine)
    calls = []
    real_open = payables.open_item

    def observe_item(ctx, **kwargs):
        assert opening._opening_item_commit.get() is ctx
        calls.append(dict(kwargs))
        return real_open(ctx, **kwargs)

    def probe(ctx, inp, key):
        assert payables._opening_approved(ctx)
        assert opening._opening_item_commit.get() is None
        result = opening.commit_opening(ctx, inp, key)
        assert len(calls) == 2
        assert opening._opening_item_commit.get() is None
        # Still in the very same approved commit, with all persisted linkage valid.
        item = dict(ctx.conn.execute(select(items).where(
            items.c.source_id == calls[0]['source_id'])).mappings().one())
        payments._source(ctx, item, item['supplier_id'], payables.payable_account(ctx),
                         set(), payments.MAX_HISTORY, {}, set())
        assert payables._opening_approved(ctx)
        # Ordinary context data cannot grant the private commit marker.
        ctx.extra['opening_item_commit'] = ctx
        with pytest.raises(Refusal) as err:
            real_open(ctx, **calls[0])
        assert err.value.code == 'payable_source_not_allowed'
        return result

    monkeypatch.setattr(payables, 'open_item', observe_item)
    monkeypatch.setattr(contract, 'commit', approval_scope._wrap_contract(probe, 'core', opening.NAME, 'commit'))
    committed(engine, card)
    assert len(rows(engine)) == 2
    before = snapshot(engine)
    with engine.begin() as conn:
        with pytest.raises(Refusal) as err:
            real_open(company_ctx(conn), **calls[0])
        assert err.value.code == 'payable_source_not_allowed'
    assert changes(before, snapshot(engine)) == {}


def test_refused_opening_preserves_existing_op_series_and_clears_item_scope(engine, monkeypatch):
    from erpclaw_core import opening_position as opening, payables

    setup(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        year = conn.execute(select(m.fiscal_year.c.label).where(
            m.fiscal_year.c.company_id == ctx.company_id).order_by(m.fiscal_year.c.start_date).limit(1)).scalar_one()
        conn.execute(m.number_series.insert().values(
            company_id=ctx.company_id, series='OP', fiscal_year=year, next_value=7))
    card = propose(engine)
    real_open = payables.open_item

    def refuse_after_item(ctx, **kwargs):
        real_open(ctx, **kwargs)
        raise Refusal('The supplier starting figure changed.', 'conflict')

    with monkeypatch.context() as failing:
        failing.setattr(payables, 'open_item', refuse_after_item)
        before = snapshot(engine)
        out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == 'refused' and out.data['code'] == 'conflict'
    assert opening._opening_item_commit.get() is None
    assert set(changes(before, snapshot(engine))) == {'approval', 'operation_outcome'}
    with engine.connect() as conn:
        assert conn.execute(select(m.number_series.c.next_value).where(
            m.number_series.c.company_id == ctx.company_id, m.number_series.c.series == 'OP',
            m.number_series.c.fiscal_year == year)).scalar_one() == 7
    assert committed(engine, propose(engine)).data['number'] == f'OP-{year}-00007'
