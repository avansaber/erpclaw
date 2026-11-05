"""the account's normal side and the stock and exchange roles

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29
Data class: schema and backfill

What it changes:
- `account.normal_side`: every account states its normal balance, debit or
  credit. Existing rows take it from their root type: asset and expense are
  debit, liability, equity and income credit.
- `account.account_type` widens to 40 characters.
- `ck_account_type` allows six more roles: stock, cost_of_goods_sold,
  stock_received_not_billed, sales_returns, exchange_gain_loss_realised and
  exchange_gain_loss_unrealised; `ck_account_role_side` fixes each of them to
  its root type and normal side.
No existing row can hold a new role, so the new checks hold on older books.
It adds no accounts: a company made before it has none of the new roles.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

# The checks as this revision makes them, written out so a later change to the models never changes this revision.
ROLE_SIDES = (
    ("stock", "asset", "debit"),
    ("cost_of_goods_sold", "expense", "debit"),
    ("stock_received_not_billed", "liability", "credit"),
    ("sales_returns", "income", "debit"),
    ("exchange_gain_loss_realised", "expense", "debit"),
    ("exchange_gain_loss_unrealised", "expense", "debit"),
)
ACCOUNT_TYPES = ("receivable", "payable", "bank", "rounding", "retained_earnings", "income",
                 *(role for role, _, _ in ROLE_SIDES))


def _quoted(values) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.add_column('account', sa.Column('normal_side', sa.String(length=6), nullable=True))
    op.execute("UPDATE account SET normal_side = CASE WHEN root_type IN ('asset','expense') THEN 'debit' "
               "ELSE 'credit' END")
    op.alter_column('account', 'normal_side', existing_type=sa.String(length=6), nullable=False)
    op.create_check_constraint('ck_account_normal_side', 'account', "normal_side in ('debit','credit')")
    op.alter_column('account', 'account_type', existing_type=sa.String(length=20), type_=sa.String(length=40),
                    existing_nullable=True)
    op.drop_constraint('ck_account_type', 'account', type_='check')
    op.create_check_constraint('ck_account_type', 'account',
                               f"account_type is null or account_type in ({_quoted(ACCOUNT_TYPES)})")
    op.create_check_constraint(
        'ck_account_role_side', 'account',
        f"account_type is null or account_type not in ({_quoted(r for r, _, _ in ROLE_SIDES)}) or "
        + " or ".join(f"(account_type = '{role}' and root_type = '{root}' and normal_side = '{side}')"
                      for role, root, side in ROLE_SIDES))


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")
