"""stock-backed sales: where an invoice's stock came from, sales orders and their deliveries, credit notes

Revision ID: sales_0003
Revises: sales_0002
Create Date: 2026-10-31
Data class: schema

PostgreSQL only (the books run on PostgreSQL). What it changes:
- An invoice line gains its place on the invoice (`line_no`), the warehouse
  the owner named for a stock line (`named_warehouse_id`) and its item's kind
  when it was written (`item_kind`); older lines have none of them. A line is
  still never updated.
- `sales_invoice_issue`: where each stock line of an invoice was taken from at
  its submission, its quantity, cost and stock entry.
- `sales_order` with its lines, `sales_delivery` with its lines (each with the
  warehouse, cost and stock entry of its issue), and `sales_invoice_delivery`:
  the delivery lines an invoice line covers.
- `sales_credit_note` with its lines, each against one invoice line, and
  `sales_credit_note_return`: each part of a line's stock that came back,
  against the issue it left by.
- An invoice may be `credited`. Its outstanding equals the total less the
  allocations of payments still standing and less its credit notes; a submitted
  invoice may owe less than its total once credited, and a credited one owes
  nothing.
- The new documents are append-only; an order's status only moves forward.
Existing invoices keep their statuses and outstanding: none has a credit note.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

from erpclaw_core.db import rate_check, scale_check, sign_check

revision = 'sales_0003'
down_revision = 'sales_0002'
branch_labels = None
depends_on = None
creates = ('sales_invoice_issue', 'sales_order', 'sales_order_line', 'sales_delivery', 'sales_delivery_line',
           'sales_invoice_delivery', 'sales_credit_note', 'sales_credit_note_line', 'sales_credit_note_return')

# As this revision makes them, written out so a later change to the models never changes it.
INVOICE_STATUSES = "status in ('draft','submitted','partly_paid','paid','credited','cancelled')"
ORDER_STATUSES = "status in ('submitted','partly_delivered','delivered','completed')"
INVOICE_FIXED = ("number", "customer_id", "posting_date", "due_date", "currency", "total", "operation_key",
                 "submit_operation_key", "created_at")
ORDER_FIXED = ("number", "customer_id", "posting_date", "delivery_date", "currency", "total", "operation_key",
               "created_at")
OPEN = "('submitted', 'partly_paid', 'paid', 'credited')"
LIVE = ("COALESCE((SELECT sum(a.amount::numeric) FROM sales_payment_allocation a JOIN sales_payment p "
        "ON p.company_id = a.company_id AND p.id = a.payment_id WHERE a.company_id = NEW.company_id "
        "AND a.invoice_id = NEW.id AND p.status = 'submitted'), 0)")
CREDITS = ("COALESCE((SELECT sum(c.total::numeric) FROM sales_credit_note c WHERE c.company_id = NEW.company_id "
           "AND c.invoice_id = NEW.id), 0)")
APPEND_ONLY = ('sales_invoice_issue', 'sales_order_line', 'sales_delivery', 'sales_delivery_line',
               'sales_invoice_delivery', 'sales_credit_note', 'sales_credit_note_line', 'sales_credit_note_return')


def _amount(table, column, sign):
    return [sa.CheckConstraint(scale_check(column, "currency"), name=f"ck_{table}_{column}_scale"),
            sa.CheckConstraint(sign_check(column, sign), name=f"ck_{table}_{column}_sign")]


def _key(table, column, target):
    return sa.ForeignKeyConstraint(['company_id', column], [f'{target}.company_id', f'{target}.id'],
                                   name=f"fk_{table}_{column.removesuffix('_id')}")


def _id(table):
    return [sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('company_id', sa.String(length=36), nullable=False)], [
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('company_id', 'id', name=f'uq_{table}_company_id')]


def _stock_cost(table, issued=True, stocked=True):
    return [sa.Column('quantity', sa.String(length=40), nullable=False),
            sa.Column('cost', sa.String(length=40), nullable=False),
            sa.Column('stock_entry_id', sa.String(length=36), nullable=not stocked),
            *([sa.Column('stock_line', sa.Integer(), nullable=not stocked)] if issued else []),
            sa.CheckConstraint(rate_check('quantity'), name=f'ck_{table}_quantity'), *_amount(table, 'cost', 'not_negative')]


def _table(name, *parts):
    cols, keys = _id(name)
    op.create_table(name, *cols, *parts, *keys)


def _document(name, *columns):
    """A numbered document of the company: its customer, currency and operation key."""
    _table(name, sa.Column('number', sa.String(length=30), nullable=False), *columns,
           sa.Column('operation_key', sa.String(length=36), nullable=True),
           sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
           sa.ForeignKeyConstraint(['company_id'], ['company.id']),
           sa.UniqueConstraint('company_id', 'number', name=f'uq_{name}_number'),
           sa.UniqueConstraint('operation_key', name=f'uq_{name}_operation_key'))


def _priced_line(name, parent, parent_column, *extra):
    _table(name, sa.Column(parent_column, sa.String(length=36), nullable=False),
           sa.Column('currency', sa.String(length=3), nullable=False),
           sa.Column('line_no', sa.Integer(), nullable=False), *extra,
           sa.Column('item_id', sa.String(length=36), nullable=False),
           sa.Column('quantity', sa.String(length=40), nullable=False),
           sa.Column('rate', sa.String(length=40), nullable=False),
           sa.Column('amount', sa.String(length=40), nullable=False),
           sa.ForeignKeyConstraint(['company_id', parent_column, 'currency'],
                                   [f'{parent}.company_id', f'{parent}.id', f'{parent}.currency'],
                                   name=f"fk_{name}_{parent.removeprefix('sales_').removeprefix('credit_')}"),
           sa.ForeignKeyConstraint(['company_id', 'item_id'], ['item.company_id', 'item.id'], name=f'fk_{name}_item'),
           sa.UniqueConstraint(parent_column, 'line_no', name=f'uq_{name}_no'),
           sa.CheckConstraint(rate_check('quantity'), name=f'ck_{name}_quantity'),
           sa.CheckConstraint(rate_check('rate'), name=f'ck_{name}_rate'))


def _invoice_changes() -> None:
    op.add_column('sales_invoice_line', sa.Column('line_no', sa.Integer(), nullable=True))
    op.add_column('sales_invoice_line', sa.Column('named_warehouse_id', sa.String(length=36), nullable=True))
    op.add_column('sales_invoice_line', sa.Column('item_kind', sa.String(length=8), nullable=True))
    op.drop_constraint('ck_inv_status', 'sales_invoice', type_='check')
    op.create_check_constraint('ck_inv_status', 'sales_invoice', INVOICE_STATUSES)
    op.drop_constraint('ck_sales_invoice_submitted_owes_total', 'sales_invoice', type_='check')
    op.create_check_constraint('ck_sales_invoice_submitted_owes', 'sales_invoice',
                               "status <> 'submitted' OR (outstanding::numeric > 0 "
                               "AND outstanding::numeric <= total::numeric)")
    op.create_check_constraint('ck_sales_invoice_credited_owes_nothing', 'sales_invoice',
                               "status <> 'credited' OR outstanding::numeric = 0")


def _tables() -> None:
    _table('sales_invoice_issue', sa.Column('invoice_line_id', sa.String(length=36), nullable=False),
           sa.Column('currency', sa.String(length=3), nullable=False),
           sa.Column('warehouse_id', sa.String(length=36), nullable=False), *_stock_cost('sales_invoice_issue'),
           _key('sales_invoice_issue', 'invoice_line_id', 'sales_invoice_line'),
           sa.UniqueConstraint('invoice_line_id', name='uq_sales_invoice_issue_line'))
    _document('sales_order', sa.Column('customer_id', sa.String(length=36), nullable=False),
              sa.Column('posting_date', sa.Date(), nullable=False),
              sa.Column('delivery_date', sa.Date(), nullable=False),
              sa.Column('currency', sa.String(length=3), nullable=False),
              sa.Column('total', sa.String(length=40), nullable=False),
              sa.Column('status', sa.String(length=16), nullable=False),
              sa.ForeignKeyConstraint(['currency'], ['currency.code']),
              sa.ForeignKeyConstraint(['company_id', 'customer_id'], ['party.company_id', 'party.id'],
                                      name='fk_sales_order_customer'),
              sa.UniqueConstraint('company_id', 'id', 'currency', name='uq_sales_order_currency_key'),
              sa.CheckConstraint(ORDER_STATUSES, name='ck_sales_order_status'),
              *_amount('sales_order', 'total', 'positive'))
    _priced_line('sales_order_line', 'sales_order', 'order_id', *_amount('sales_order_line', 'amount', 'positive'))
    _document('sales_delivery', sa.Column('order_id', sa.String(length=36), nullable=False),
              sa.Column('posting_date', sa.Date(), nullable=False),
              _key('sales_delivery', 'order_id', 'sales_order'))
    _table('sales_delivery_line', sa.Column('delivery_id', sa.String(length=36), nullable=False),
           sa.Column('order_line_id', sa.String(length=36), nullable=False),
           sa.Column('currency', sa.String(length=3), nullable=False),
           sa.Column('warehouse_id', sa.String(length=36), nullable=True),
           *_stock_cost('sales_delivery_line', stocked=False),
           _key('sales_delivery_line', 'delivery_id', 'sales_delivery'),
           _key('sales_delivery_line', 'order_line_id', 'sales_order_line'))
    _table('sales_invoice_delivery', sa.Column('invoice_line_id', sa.String(length=36), nullable=False),
           sa.Column('delivery_line_id', sa.String(length=36), nullable=False),
           sa.Column('quantity', sa.String(length=40), nullable=False),
           _key('sales_invoice_delivery', 'invoice_line_id', 'sales_invoice_line'),
           _key('sales_invoice_delivery', 'delivery_line_id', 'sales_delivery_line'),
           sa.CheckConstraint(rate_check('quantity'), name='ck_sales_invoice_delivery_quantity'))
    _document('sales_credit_note', sa.Column('invoice_id', sa.String(length=36), nullable=False),
              sa.Column('customer_id', sa.String(length=36), nullable=False),
              sa.Column('posting_date', sa.Date(), nullable=False),
              sa.Column('currency', sa.String(length=3), nullable=False),
              sa.Column('total', sa.String(length=40), nullable=False),
              sa.ForeignKeyConstraint(['currency'], ['currency.code']),
              sa.ForeignKeyConstraint(['company_id', 'invoice_id', 'currency'],
                                      ['sales_invoice.company_id', 'sales_invoice.id', 'sales_invoice.currency'],
                                      name='fk_sales_credit_note_invoice'),
              sa.ForeignKeyConstraint(['company_id', 'customer_id'], ['party.company_id', 'party.id'],
                                      name='fk_sales_credit_note_customer'),
              sa.UniqueConstraint('company_id', 'id', 'currency', name='uq_sales_credit_note_currency_key'),
              *_amount('sales_credit_note', 'total', 'positive'))
    _priced_line('sales_credit_note_line', 'sales_credit_note', 'credit_note_id',
                 sa.Column('invoice_line_id', sa.String(length=36), nullable=False),
                 _key('sales_credit_note_line', 'invoice_line_id', 'sales_invoice_line'),
                 *_amount('sales_credit_note_line', 'amount', 'not_negative'))
    _table('sales_credit_note_return', sa.Column('credit_note_line_id', sa.String(length=36), nullable=False),
           sa.Column('currency', sa.String(length=3), nullable=False),
           sa.Column('original_voucher_type', sa.String(length=40), nullable=False),
           sa.Column('original_voucher_id', sa.String(length=36), nullable=False),
           sa.Column('original_line', sa.Integer(), nullable=False),
           sa.Column('warehouse_id', sa.String(length=36), nullable=False),
           *_stock_cost('sales_credit_note_return', issued=False),
           _key('sales_credit_note_return', 'credit_note_line_id', 'sales_credit_note_line'))


def _changed(columns) -> str:
    return " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in columns)


def _triggers(schema: str) -> None:
    # SECURITY DEFINER with the books' schema pinned, as sales_0002: the invoice rule reads payments and credit
    # notes the runtime role may not read in full, and no session's temporary table can stand in for them.
    path = f"SET search_path = {schema}, pg_temp"
    transitions = ("NEW.status = OLD.status OR (OLD.status = 'draft' AND NEW.status IN ('submitted', 'cancelled')) "
                   f"OR (OLD.status IN {OPEN} AND NEW.status IN {OPEN}) "
                   "OR (OLD.status = 'submitted' AND NEW.status = 'cancelled')")
    op.execute(f"""CREATE OR REPLACE FUNCTION erpclaw_sales_invoice_update() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER {path}
AS $$ BEGIN
  IF NOT ({transitions}) OR {_changed(('id', 'company_id'))}
     OR (OLD.status <> 'draft' AND ({_changed(INVOICE_FIXED)}))
     OR (OLD.cancel_operation_key IS NOT NULL AND NEW.cancel_operation_key IS DISTINCT FROM OLD.cancel_operation_key)
  THEN RAISE EXCEPTION 'sales_invoice: that change is refused once it is submitted; status only moves forward, except back among submitted, partly paid, paid and credited as payments and credits move, and never to a draft';
  END IF;
  IF NEW.status IN {OPEN} AND NEW.outstanding::numeric <> NEW.total::numeric - {LIVE} - {CREDITS} THEN
    RAISE EXCEPTION 'sales_invoice: outstanding must be the total less the allocations of payments still standing and its credit notes';
  END IF;
  IF NEW.status = 'cancelled' AND OLD.status = 'submitted' AND ({LIVE} <> 0 OR {CREDITS} <> 0
     OR NOT EXISTS (SELECT 1 FROM posting o JOIN posting r ON r.company_id = o.company_id AND r.reverses_posting_id = o.id
                    WHERE o.company_id = NEW.company_id AND o.voucher_type = 'sales_invoice' AND o.voucher_id = NEW.id)) THEN
    RAISE EXCEPTION 'sales_invoice: a submitted invoice is cancelled only with no payment or credit note standing and its posting reversed';
  END IF;
  RETURN NEW;
END $$""")
    forward = ("NEW.status = OLD.status OR (OLD.status = 'submitted' AND NEW.status IN "
               "('partly_delivered', 'delivered', 'completed')) OR (OLD.status = 'partly_delivered' AND NEW.status IN "
               "('delivered', 'completed')) OR (OLD.status = 'delivered' AND NEW.status = 'completed')")
    op.execute(f"""CREATE FUNCTION erpclaw_sales_order_update() RETURNS trigger LANGUAGE plpgsql {path}
AS $$ BEGIN
  IF NOT ({forward}) OR {_changed(('id', 'company_id') + ORDER_FIXED)} THEN
    RAISE EXCEPTION 'sales_order: only its status changes, and only forward';
  END IF;
  RETURN NEW;
END $$""")
    op.execute("CREATE TRIGGER sales_order_update BEFORE UPDATE ON sales_order "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_sales_order_update()")
    op.execute("CREATE TRIGGER sales_order_delete BEFORE DELETE ON sales_order "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_sales_refuse()")
    for table in APPEND_ONLY:
        op.execute(f"CREATE TRIGGER {table}_no_change BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION erpclaw_sales_refuse()")
    for table in creates:  # TRUNCATE fires no row triggers
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION erpclaw_sales_refuse()")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("the books run on PostgreSQL only")
    _invoice_changes()
    _tables()
    _triggers(bind.execute(sa.text("SELECT quote_ident(current_schema())")).scalar_one())


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")
