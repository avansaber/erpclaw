"""the sales tables, to the core books rules, and the triggers that keep a submitted document fixed

Revision ID: sales_0001
Revises: (base of the sales branch)
Create Date: 2026-10-28
Data class: schema

The base of the sales plugin's branch:
its own base, no depends_on; the engine checks that the books' core revision
is at or after the manifest's (0004, which gives item its company key).
Composite company keys on every reference, amounts at the currency's scale,
numbers unique per company. Triggers, the second layer behind the runtime
role's privileges (the books run on PostgreSQL only):
- an invoice is inserted only as a draft, and a line only into a draft
  (on PostgreSQL the line reads its invoice FOR SHARE, so it waits for a
  submission holding the row and then sees it submitted);
- an invoice's status only moves forward, and once it is not a draft only
  its status, outstanding and cancel key change; a payment likewise;
- lines and allocations are never updated or deleted, an invoice is deleted
  only as a draft and a payment never; ON CONFLICT DO UPDATE fires the update
  rule;
- TRUNCATE is refused, and the trigger functions resolve tables
  in the books' schema only, never a session's temporary tables.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

from erpclaw_core.db import rate_check, scale_check, sign_check

revision = 'sales_0001'
down_revision = None
branch_labels = ('sales',)
depends_on = None
creates = ('sales_invoice', 'sales_invoice_line', 'sales_payment', 'sales_payment_allocation')

TABLES = creates
INVOICE_FORWARD = ("NEW.status = OLD.status OR (OLD.status = 'draft' AND NEW.status IN ('submitted','cancelled')) "
                   "OR (OLD.status = 'submitted' AND NEW.status IN ('partly_paid','paid','cancelled')) "
                   "OR (OLD.status = 'partly_paid' AND NEW.status = 'paid')")
# once an invoice is not a draft, only status, outstanding and the cancel key change
INVOICE_FIXED = ("number", "customer_id", "posting_date", "due_date", "currency", "total", "operation_key",
                 "submit_operation_key", "created_at")
PAYMENT_FORWARD = "NEW.status = OLD.status OR (OLD.status = 'submitted' AND NEW.status = 'cancelled')"
PAYMENT_FIXED = ("number", "customer_id", "posting_date", "currency", "amount", "unallocated", "method",
                 "reference", "operation_key", "created_at")


def _sign(column, sign):
    return sign_check(column, sign)


def _amount(table, column, sign):
    return [sa.CheckConstraint(scale_check(column, "currency"), name=f"ck_{table}_{column}_scale"),
            sa.CheckConstraint(_sign(column, sign), name=f"ck_{table}_{column}_sign")]


def _tables() -> None:
    op.create_table('sales_invoice',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('number', sa.String(length=30), nullable=False),
        sa.Column('customer_id', sa.String(length=36), nullable=False),
        sa.Column('posting_date', sa.Date(), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('total', sa.String(length=40), nullable=False),
        sa.Column('outstanding', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=14), nullable=False),
        sa.Column('operation_key', sa.String(length=36), nullable=True),
        sa.Column('submit_operation_key', sa.String(length=36), nullable=True),
        sa.Column('cancel_operation_key', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['company.id']),
        sa.ForeignKeyConstraint(['currency'], ['currency.code']),
        sa.ForeignKeyConstraint(['company_id', 'customer_id'], ['party.company_id', 'party.id'],
                                name='fk_sales_invoice_customer'),
        sa.UniqueConstraint('company_id', 'id', name='uq_sales_invoice_company_id'),
        sa.UniqueConstraint('company_id', 'id', 'currency', name='uq_sales_invoice_currency_key'),
        sa.UniqueConstraint('company_id', 'number', name='uq_sales_invoice_number'),
        sa.UniqueConstraint('operation_key', name='uq_sales_invoice_operation_key'),
        sa.UniqueConstraint('submit_operation_key', name='uq_sales_invoice_submit_key'),
        sa.UniqueConstraint('cancel_operation_key', name='uq_sales_invoice_cancel_key'),
        sa.CheckConstraint("status in ('draft','submitted','partly_paid','paid','cancelled')", name='ck_inv_status'),
        *_amount('sales_invoice', 'total', 'positive'),
        *_amount('sales_invoice', 'outstanding', 'not_negative'),
        sa.CheckConstraint(f"status <> 'draft' OR ({_sign('outstanding', 'zero')})",
                           name='ck_sales_invoice_draft_owes_nothing'),
    )
    op.create_table('sales_invoice_line',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('invoice_id', sa.String(length=36), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('item_id', sa.String(length=36), nullable=False),
        sa.Column('quantity', sa.String(length=40), nullable=False),
        sa.Column('rate', sa.String(length=40), nullable=False),
        sa.Column('amount', sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id', 'invoice_id', 'currency'],
                                ['sales_invoice.company_id', 'sales_invoice.id', 'sales_invoice.currency'],
                                name='fk_sales_invoice_line_invoice'),
        sa.ForeignKeyConstraint(['company_id', 'item_id'], ['item.company_id', 'item.id'],
                                name='fk_sales_invoice_line_item'),
        sa.UniqueConstraint('company_id', 'id', name='uq_sales_invoice_line_company_id'),
        sa.CheckConstraint(rate_check('quantity'), name='ck_sales_invoice_line_quantity'),
        sa.CheckConstraint(rate_check('rate'), name='ck_sales_invoice_line_rate'),
        *_amount('sales_invoice_line', 'amount', 'positive'),
    )
    op.create_table('sales_payment',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('number', sa.String(length=30), nullable=False),
        sa.Column('customer_id', sa.String(length=36), nullable=False),
        sa.Column('posting_date', sa.Date(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('amount', sa.String(length=40), nullable=False),
        sa.Column('unallocated', sa.String(length=40), nullable=False),
        sa.Column('method', sa.String(length=60), nullable=True),
        sa.Column('reference', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('operation_key', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['company.id']),
        sa.ForeignKeyConstraint(['currency'], ['currency.code']),
        sa.ForeignKeyConstraint(['company_id', 'customer_id'], ['party.company_id', 'party.id'],
                                name='fk_sales_payment_customer'),
        sa.UniqueConstraint('company_id', 'id', name='uq_sales_payment_company_id'),
        sa.UniqueConstraint('company_id', 'id', 'currency', name='uq_sales_payment_currency_key'),
        sa.UniqueConstraint('company_id', 'number', name='uq_sales_payment_number'),
        sa.UniqueConstraint('operation_key', name='uq_sales_payment_operation_key'),
        sa.CheckConstraint("status in ('submitted','cancelled')", name='ck_pay_status'),
        *_amount('sales_payment', 'amount', 'positive'),
        *_amount('sales_payment', 'unallocated', 'not_negative'),
    )
    op.create_table('sales_payment_allocation',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('payment_id', sa.String(length=36), nullable=False),
        sa.Column('invoice_id', sa.String(length=36), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('amount', sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id', 'payment_id', 'currency'],
                                ['sales_payment.company_id', 'sales_payment.id', 'sales_payment.currency'],
                                name='fk_sales_allocation_payment'),
        sa.ForeignKeyConstraint(['company_id', 'invoice_id', 'currency'],
                                ['sales_invoice.company_id', 'sales_invoice.id', 'sales_invoice.currency'],
                                name='fk_sales_allocation_invoice'),
        sa.UniqueConstraint('company_id', 'id', name='uq_sales_allocation_company_id'),
        *_amount('sales_payment_allocation', 'amount', 'positive'),
    )


def _changed(columns, op_new="NEW", op_old="OLD", distinct="IS NOT") -> str:
    return " OR ".join(f"{op_new}.{c} {distinct} {op_old}.{c}" for c in columns)


def _postgres_triggers(schema: str) -> None:
    # Pinned search_path: the books' schema, then pg_temp last, so a session's temporary table can never
    # stand in for sales_invoice (PostgreSQL searches pg_temp first unless it is named).
    path = f"SET search_path = {schema}, pg_temp"

    def function(name, body):
        op.execute(f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql {path} AS $$ BEGIN {body} END $$")

    def trigger(name, timing, table, fn, level="ROW"):
        op.execute(f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH {level} EXECUTE FUNCTION {fn}()")

    function("erpclaw_sales_refuse", "RAISE EXCEPTION '%: % refused', TG_TABLE_NAME, TG_OP;")
    function("erpclaw_sales_invoice_insert",
             "IF NEW.status <> 'draft' THEN RAISE EXCEPTION 'sales_invoice: an invoice is created as a draft'; "
             "END IF; RETURN NEW;")
    function("erpclaw_sales_invoice_update",
             f"IF NOT ({INVOICE_FORWARD}) OR {_changed(('id', 'company_id'), distinct='IS DISTINCT FROM')} "
             f"OR (OLD.status <> 'draft' AND ({_changed(INVOICE_FIXED, distinct='IS DISTINCT FROM')})) "
             "OR (OLD.cancel_operation_key IS NOT NULL "
             "AND NEW.cancel_operation_key IS DISTINCT FROM OLD.cancel_operation_key) THEN "
             "RAISE EXCEPTION 'sales_invoice: that change is refused once it is submitted, and status only moves "
             "forward'; END IF; RETURN NEW;")
    function("erpclaw_sales_invoice_delete",
             "IF OLD.status <> 'draft' THEN RAISE EXCEPTION 'sales_invoice: only a draft can be deleted'; END IF; "
             "RETURN OLD;")
    function("erpclaw_sales_line_into_draft",
             "IF (SELECT status FROM sales_invoice WHERE company_id = NEW.company_id AND id = NEW.invoice_id "
             "FOR SHARE) IS DISTINCT FROM 'draft' THEN "
             "RAISE EXCEPTION 'sales_invoice_line: a line goes only into a draft invoice'; END IF; RETURN NEW;")
    function("erpclaw_sales_payment_update",
             f"IF NOT ({PAYMENT_FORWARD}) OR "
             f"{_changed(('id', 'company_id') + PAYMENT_FIXED, distinct='IS DISTINCT FROM')} THEN "
             "RAISE EXCEPTION 'sales_payment: that change is refused, and status only moves forward'; END IF; "
             "RETURN NEW;")
    trigger("sales_invoice_insert_draft", "BEFORE INSERT", "sales_invoice", "erpclaw_sales_invoice_insert")
    trigger("sales_invoice_update", "BEFORE UPDATE", "sales_invoice", "erpclaw_sales_invoice_update")
    trigger("sales_invoice_delete", "BEFORE DELETE", "sales_invoice", "erpclaw_sales_invoice_delete")
    trigger("sales_invoice_line_into_draft", "BEFORE INSERT", "sales_invoice_line", "erpclaw_sales_line_into_draft")
    trigger("sales_payment_update", "BEFORE UPDATE", "sales_payment", "erpclaw_sales_payment_update")
    trigger("sales_payment_delete", "BEFORE DELETE", "sales_payment", "erpclaw_sales_refuse")
    for table in ("sales_invoice_line", "sales_payment_allocation"):
        trigger(f"{table}_no_change", "BEFORE UPDATE OR DELETE", table, "erpclaw_sales_refuse")
    for table in TABLES:  # TRUNCATE fires no row triggers
        trigger(f"{table}_no_truncate", "BEFORE TRUNCATE", table, "erpclaw_sales_refuse", level="STATEMENT")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("the books run on PostgreSQL only")
    _tables()
    schema = bind.execute(sa.text("SELECT quote_ident(current_schema())")).scalar_one()
    _postgres_triggers(schema)


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")
