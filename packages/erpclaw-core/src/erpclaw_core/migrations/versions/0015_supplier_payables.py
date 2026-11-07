"""supplier payables: items, payments and their allocations; the payable-source rule; the stock-in roles

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-02
Data class: schema

Execution waits for the complete migration chain.

What it changes:
- `supplier_payable`: one item per still-owed delivery, with the source voucher's type, id and number, its date,
  amount, outstanding and status (open, paid or reversed). The database refuses an UPDATE of any column but
  `outstanding` and `status`, every DELETE and TRUNCATE; open exactly when something is outstanding, never more than
  the amount.
- `supplier_payment`: a payment's supplier, date, amount, paying account, number and status; `unallocated` is held
  at zero by a CHECK in v3.0. Only `status` and `reverse_operation_key` change, and a
  reversed payment never changes again; DELETE and TRUNCATE are refused.
- `supplier_payment_allocation`: insert only (UPDATE, DELETE and TRUNCATE refused).
- The payable-source rule's backstop: a ledger line naming a party on an account of role `payable` is refused
  unless its posting's voucher type is a still-owed delivery's or a supplier payment's (or their reversals). The
  posting service refuses it first, in words (`payable_source_not_allowed`); this catches every other writer.
  No existing row can break it: no core voucher posted a supplier line before this revision.
- `ck_account_type` allows three more roles, `opening_balance`, `stock_adjustment` and `stock_write_off`, and
  `ck_account_role_side` fixes them to equity and credit, expense and debit, expense and debit. No existing row can
  hold a new role, so the checks hold on older books. It adds no accounts.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

from erpclaw_core.db import scale_check, sign_check

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

# Written out, so a later change to the models never changes this revision.
ROLE_SIDES = (
    ("stock", "asset", "debit"),
    ("cost_of_goods_sold", "expense", "debit"),
    ("stock_received_not_billed", "liability", "credit"),
    ("sales_returns", "income", "debit"),
    ("exchange_gain_loss_realised", "expense", "debit"),
    ("exchange_gain_loss_unrealised", "expense", "debit"),
    ("opening_balance", "equity", "credit"),
    ("stock_adjustment", "expense", "debit"),
    ("stock_write_off", "expense", "debit"),
)
ACCOUNT_TYPES = ("receivable", "payable", "bank", "rounding", "retained_earnings", "income",
                 *(role for role, _, _ in ROLE_SIDES))
PAYABLE_SOURCES = ("stock_goods_received", "stock_goods_received:reversal", "supplier_payment",
                   "supplier_payment:reversal")


def _quoted(values) -> str:
    return ",".join(f"'{v}'" for v in values)


def _scale(column: str) -> str:
    return scale_check(column, "currency")  # as revision 0004 takes rate_check: the models' own check


def _sign(column: str, sign: str) -> str:
    return sign_check(column, sign)


def _amount(table: str, column: str, sign: str) -> list:
    return [sa.CheckConstraint(_scale(column), name=f"ck_{table}_{column}_scale"),
            sa.CheckConstraint(_sign(column, sign), name=f"ck_{table}_{column}_sign")]


def _refuse(table: str, ops: str) -> None:
    op.execute(f"CREATE TRIGGER {table}_no_{ops.split()[0].lower()} BEFORE {ops} ON {table} "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_refuse_change()")


def _no_truncate(table: str) -> None:
    op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
               "FOR EACH STATEMENT EXECUTE FUNCTION erpclaw_refuse_change()")


def upgrade() -> None:
    op.drop_constraint('ck_account_type', 'account', type_='check')
    op.create_check_constraint('ck_account_type', 'account',
                               f"account_type is null or account_type in ({_quoted(ACCOUNT_TYPES)})")
    op.drop_constraint('ck_account_role_side', 'account', type_='check')
    op.create_check_constraint(
        'ck_account_role_side', 'account',
        f"account_type is null or account_type not in ({_quoted(r for r, _, _ in ROLE_SIDES)}) or "
        + " or ".join(f"(account_type = '{role}' and root_type = '{root}' and normal_side = '{side}')"
                      for role, root, side in ROLE_SIDES))

    op.create_table(
        'supplier_payable',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('company_id', sa.String(length=36), sa.ForeignKey('company.id'), nullable=False),
        sa.Column('supplier_id', sa.String(length=36), nullable=False),
        sa.Column('source_type', sa.String(length=40), nullable=False),
        sa.Column('source_id', sa.String(length=36), nullable=False),
        sa.Column('source_number', sa.String(length=30), nullable=False),
        sa.Column('posting_date', sa.Date(), nullable=False),
        sa.Column('currency', sa.String(length=3), sa.ForeignKey('currency.code'), nullable=False),
        sa.Column('amount', sa.String(length=40), nullable=False),
        sa.Column('outstanding', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['company_id', 'supplier_id'], ['party.company_id', 'party.id'],
                                name='fk_supplier_payable_supplier'),
        sa.UniqueConstraint('company_id', 'id', 'currency', name='uq_supplier_payable_currency_key'),
        sa.UniqueConstraint('company_id', 'source_type', 'source_id', name='uq_supplier_payable_source'),
        sa.CheckConstraint("status in ('open','paid','reversed')", name='ck_supplier_payable_status'),
        *_amount('supplier_payable', 'amount', 'positive'),
        *_amount('supplier_payable', 'outstanding', 'not_negative'),
        sa.CheckConstraint("outstanding::numeric <= amount::numeric", name='ck_supplier_payable_within_amount'),
        sa.CheckConstraint("(status = 'open') = (outstanding::numeric > 0)", name='ck_supplier_payable_open_owes'),
    )
    op.create_table(
        'supplier_payment',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('company_id', sa.String(length=36), sa.ForeignKey('company.id'), nullable=False),
        sa.Column('number', sa.String(length=30), nullable=False),
        sa.Column('supplier_id', sa.String(length=36), nullable=False),
        sa.Column('posting_date', sa.Date(), nullable=False),
        sa.Column('currency', sa.String(length=3), sa.ForeignKey('currency.code'), nullable=False),
        sa.Column('amount', sa.String(length=40), nullable=False),
        sa.Column('unallocated', sa.String(length=40), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('operation_key', sa.String(length=36), nullable=True),
        sa.Column('reverse_operation_key', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['company_id', 'supplier_id'], ['party.company_id', 'party.id'],
                                name='fk_supplier_payment_supplier'),
        sa.ForeignKeyConstraint(['company_id', 'account_id'], ['account.company_id', 'account.id'],
                                name='fk_supplier_payment_account'),
        sa.UniqueConstraint('company_id', 'id', 'currency', name='uq_supplier_payment_currency_key'),
        sa.UniqueConstraint('company_id', 'number', name='uq_supplier_payment_number'),
        sa.UniqueConstraint('operation_key', name='uq_supplier_payment_operation_key'),
        sa.UniqueConstraint('reverse_operation_key', name='uq_supplier_payment_reverse_key'),
        sa.CheckConstraint("status in ('posted','reversed')", name='ck_supplier_payment_status'),
        *_amount('supplier_payment', 'amount', 'positive'),
        sa.CheckConstraint(_scale('unallocated'), name='ck_supplier_payment_unallocated_scale'),
        sa.CheckConstraint(_sign('unallocated', 'zero'), name='ck_supplier_payment_unallocated_zero'),
    )
    op.create_table(
        'supplier_payment_allocation',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('company_id', sa.String(length=36), nullable=False),
        sa.Column('payment_id', sa.String(length=36), nullable=False),
        sa.Column('payable_id', sa.String(length=36), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('amount', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['company_id', 'payment_id', 'currency'],
                                ['supplier_payment.company_id', 'supplier_payment.id', 'supplier_payment.currency'],
                                name='fk_supplier_allocation_payment'),
        sa.ForeignKeyConstraint(['company_id', 'payable_id', 'currency'],
                                ['supplier_payable.company_id', 'supplier_payable.id', 'supplier_payable.currency'],
                                name='fk_supplier_allocation_payable'),
        sa.UniqueConstraint('payment_id', 'payable_id', name='uq_supplier_allocation_item'),
        *_amount('supplier_payment_allocation', 'amount', 'positive'),
    )

    # Only the named columns of a row may change: every other column of NEW equals OLD.
    op.execute(
        "CREATE OR REPLACE FUNCTION erpclaw_only_columns() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "DECLARE n jsonb := to_jsonb(NEW); o jsonb := to_jsonb(OLD); i int; BEGIN "
        "FOR i IN 0 .. TG_NARGS - 1 LOOP n := n - TG_ARGV[i]; o := o - TG_ARGV[i]; END LOOP; "
        "IF n IS DISTINCT FROM o THEN RAISE EXCEPTION 'table %: only % may change', TG_TABLE_NAME, "
        "array_to_string(TG_ARGV, ', '); END IF; RETURN NEW; END $$")
    op.execute("CREATE TRIGGER supplier_payable_columns BEFORE UPDATE ON supplier_payable "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_only_columns('outstanding', 'status')")
    _refuse('supplier_payable', 'DELETE')
    _no_truncate('supplier_payable')
    op.execute("CREATE TRIGGER supplier_payment_columns BEFORE UPDATE ON supplier_payment "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_only_columns('status', 'reverse_operation_key')")
    op.execute("CREATE TRIGGER supplier_payment_reversed_once BEFORE UPDATE ON supplier_payment "
               "FOR EACH ROW WHEN (OLD.status = 'reversed') EXECUTE FUNCTION erpclaw_refuse_change()")
    _refuse('supplier_payment', 'DELETE')
    _no_truncate('supplier_payment')
    _refuse('supplier_payment_allocation', 'UPDATE OR DELETE')
    _no_truncate('supplier_payment_allocation')

    # The payable-source rule's backstop, on the ledger's own rows.
    op.execute(
        "CREATE OR REPLACE FUNCTION erpclaw_payable_source() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.party_id IS NOT NULL AND EXISTS (SELECT 1 FROM account a WHERE a.id = NEW.account_id "
        "AND a.account_type = 'payable') AND COALESCE((SELECT p.voucher_type FROM posting p "
        f"WHERE p.id = NEW.posting_id), '') NOT IN ({_quoted(PAYABLE_SOURCES)}) THEN "
        "RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'payable_source_not_allowed: only a delivery "
        "still owed or a supplier payment can change what a supplier is owed (posting ' || NEW.posting_id || ')'; "
        "END IF; RETURN NEW; END $$")
    op.execute("CREATE TRIGGER ledger_entry_payable_source BEFORE INSERT ON ledger_entry "
               "FOR EACH ROW EXECUTE FUNCTION erpclaw_payable_source()")


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")
