"""a planted plugin's branch: its one table (tests only)

Revision ID: extra_0001
"""
from alembic import op
import sqlalchemy as sa

revision = 'extra_0001'
down_revision = None
branch_labels = ('extra',)
depends_on = None
creates = ('extra_thing',)


def upgrade() -> None:
    op.create_table('extra_thing', sa.Column('id', sa.String(length=36), nullable=False),
                    sa.PrimaryKeyConstraint('id'))


def downgrade() -> None:
    raise NotImplementedError
