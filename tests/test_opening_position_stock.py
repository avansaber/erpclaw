"""Opening stock shares the approved opening position posting."""
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from bookdiff import changes, snapshot
from erpclaw_core import approvals, ledger, models as m
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import service as stock, tables as st
from erpclaw_stock.opening_stock import RecordOpeningStock, opening_read_set, plan_opening


def pair(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        item = conn.execute(select(m.item).where(m.item.c.company_id == ctx.company_id, m.item.c.name == 'Copper Wire')).mappings().one()
        name = 'Warehouse ' + new_id()[:8]
        wid = stock.create_warehouse(ctx, name)
        try:
            stock.make_stock_item(ctx, item['id'], 'moving_average')
        except Refusal as exc:
            if exc.code not in ('already_stock_item', 'valuation_method_fixed'):
                raise
        return dict(item), wid, name


def args(name):
    return {'stock_lines': [{'item': 'Copper Wire', 'warehouse': name, 'quantity': '100', 'rate': '8.00'}]}


def approve(engine, data):
    card = approvals.propose(engine, REGISTRY.contracts['record_opening_position'], data)
    return approvals.approve(engine, card.approval_id, card.digest)


def test_stock_money_lives_only_in_financial_posting(engine):
    _, _, name = pair(engine)
    before = snapshot(engine)
    out = approve(engine, args(name))
    assert out.status == 'committed', out.text
    diff = changes(before, snapshot(engine))
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        posting = conn.execute(select(m.posting).where(m.posting.c.company_id == ctx.company_id, m.posting.c.voucher_type == ledger.OPENING)).mappings().one()
        assert posting['voucher_type'] == ledger.OPENING
        rows = conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.posting_id == posting['id'])).mappings().all()
        assert len(rows) == 2
        inv = ledger.account_by_type(ctx, 'stock')
        assert next(r for r in rows if r['account_id'] == inv['id'])['debit'] == Decimal('800')
        assert sum(r['debit'] for r in rows) == sum(r['credit'] for r in rows) == Decimal('800')
    assert len(diff['ledger_entry']['added']) == 2


def test_stock_quantity_voucher_carries_date_rate_and_lines(engine):
    _, _, name = pair(engine)
    out = approve(engine, args(name))
    assert out.status == 'committed', out.text
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        voucher = conn.execute(select(st.voucher).where(st.voucher.c.company_id == ctx.company_id)).mappings().one()
        line = conn.execute(select(st.voucher_line).where(st.voucher_line.c.voucher_id == voucher['id'])).mappings().one()
        assert (line['qty_moved'], line['rate'], line['value']) == (Decimal('100'), Decimal('8'), Decimal('800'))
        entry = conn.execute(select(st.entry).where(st.entry.c.company_id == ctx.company_id)).mappings().one()
        assert (entry['qty'], entry['value'], entry['posting_date']) == (Decimal('100'), Decimal('800'), voucher['posting_date'])


def test_stock_entries_link_to_financial_posting_and_verify_clean(engine):
    _, wid, name = pair(engine)
    out = approve(engine, args(name))
    assert out.status == 'committed', out.text
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        assert stock.verify_stock(ctx) == []
        posting = conn.execute(select(m.posting).where(m.posting.c.company_id == ctx.company_id, m.posting.c.voucher_type == ledger.OPENING)).mappings().one()
        entries = conn.execute(select(st.entry).where(st.entry.c.company_id == ctx.company_id)).mappings().all()
        assert entries
        assert all(stock._posting_id(ctx, e['voucher_type'], e['voucher_id']) == posting['id'] for e in entries if e['value'])
        value = sum(r['value'] for r in conn.execute(select(st.head).where(st.head.c.warehouse_id == wid)).mappings())
        assert value == Decimal('800')
        import datetime
        assert ledger.balance(ctx, ledger.account_by_type(ctx, 'stock')['id'], datetime.date.max) == value


def test_stock_duplicate_pair_refused(engine):
    _, _, name = pair(engine)
    lines = args(name)['stock_lines'] * 2
    with engine.connect() as conn:
        before = snapshot(engine)
        with pytest.raises(Refusal) as exc:
            plan_opening(company_ctx(conn), RecordOpeningStock(lines=lines))
        assert exc.value.code == 'duplicate_stock_pair'
        assert changes(before, snapshot(engine)) == {}


def test_stock_and_core_read_sets_unite_and_stale_rolls_back(engine):
    item, _, name = pair(engine)
    contract = REGISTRY.contracts['record_opening_position']
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        stock_rows = set(opening_read_set(ctx, RecordOpeningStock(lines=args(name)['stock_lines'])))
        assert stock_rows <= set(contract.read_set(ctx, contract.input_model.model_validate(args(name))))
    card = approvals.propose(engine, contract, args(name))
    with engine.begin() as conn:
        conn.execute(update(m.item).where(m.item.c.id == item['id']).values(name='Changed Copper Wire'))
    before = snapshot(engine)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == 'superseded'
    diff = changes(before, snapshot(engine))
    assert not any(table in diff for table in ('posting', 'ledger_entry', 'stock_voucher', 'stock_voucher_line', 'stock_ledger_entry', 'stock_head'))


def test_stock_plan_refuses_moved_pair(engine):
    item, wid, name = pair(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        trusted = stock.Trusted('opening test setup movement')
        stock.receive(ctx, new_id(), item['id'], wid, '1', '8.00', ctx.today, trusted=trusted)
        trusted.verify(ctx)
    with engine.connect() as conn:
        before = snapshot(engine)
        with pytest.raises(Refusal) as exc:
            plan_opening(company_ctx(conn), RecordOpeningStock(lines=args(name)['stock_lines']))
        assert exc.value.code == 'opening_stock_moved'
        assert changes(before, snapshot(engine)) == {}


@pytest.mark.parametrize(('column', 'code'), [('frozen', 'frozen_account'), ('is_group', 'not_inventory_account')])
def test_stock_plan_refuses_frozen_or_group_inventory_account(engine, column, code):
    _, _, name = pair(engine)
    with engine.begin() as conn:
        account = ledger.account_by_type(company_ctx(conn), 'stock')
        conn.execute(update(m.account).where(m.account.c.id == account['id']).values({column: True}))
    with engine.connect() as conn:
        before = snapshot(engine)
        with pytest.raises(Refusal) as exc:
            plan_opening(company_ctx(conn), RecordOpeningStock(lines=args(name)['stock_lines']))
        assert exc.value.code == code
        assert changes(before, snapshot(engine)) == {}


def test_stock_plan_maps_closed_period_to_opening_period_closed(engine):
    from erpclaw_stock.opening_stock import _books_start
    _, _, name = pair(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        _, period = _books_start(ctx)
        ledger.set_period_status(ctx, period['id'], 'locked')
    with engine.connect() as conn:
        before = snapshot(engine)
        with pytest.raises(Refusal) as exc:
            plan_opening(company_ctx(conn), RecordOpeningStock(lines=args(name)['stock_lines']))
        assert exc.value.code == 'opening_period_closed'
        assert changes(before, snapshot(engine)) == {}


def test_stock_and_supplier_share_posting_and_control_line(engine):
    from erpclaw_core import masters
    from erpclaw_core.payable_tables import supplier_payable
    _, _, name = pair(engine)
    with engine.begin() as conn:
        supplier_id = masters.create_party(company_ctx(conn), 'supplier', 'Supplier X')
    data = args(name)
    data['bank_lines'] = [{'account': 'Bank', 'amount': '5000.00'}]
    data['supplier_lines'] = [{'supplier': 'Supplier X', 'amount': '1200.00'}]
    out = approve(engine, data)
    assert out.status == 'committed', out.text
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        row = conn.execute(select(supplier_payable).where(supplier_payable.c.supplier_id == supplier_id)).mappings().one()
        lines = conn.execute(select(m.ledger_entry).where(m.ledger_entry.c.posting_id == out.data['posting_id']).order_by(m.ledger_entry.c.line_no)).mappings().all()
        assert len(lines) == 4
        assert row['posting_id'] == out.data['posting_id'] and row['control_line_no'] == 3
        assert lines[2]['party_id'] == supplier_id and lines[2]['credit'] == Decimal('1200')
        assert sum(r['debit'] for r in lines) == sum(r['credit'] for r in lines) == Decimal('5800')
        assert stock.verify_stock(ctx) == []


def test_outer_stock_maps_closed_period(engine):
    from erpclaw_stock.opening_stock import _books_start
    _, _, name = pair(engine)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        _, period = _books_start(ctx)
        ledger.set_period_status(ctx, period['id'], 'locked')
    before = snapshot(engine)
    with pytest.raises(Refusal) as exc:
        approvals.propose(engine, REGISTRY.contracts['record_opening_position'], args(name))
    assert exc.value.code == 'opening_period_closed'
    assert changes(before, snapshot(engine)) == {}
