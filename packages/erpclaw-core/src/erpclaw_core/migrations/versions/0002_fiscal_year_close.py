"""fiscal year close records

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27
Data class: schema

One row per closed fiscal year: who closed it, when, and its closing
posting (none when every income and expense balance was zero). A table of
its own rather than columns on fiscal_year. No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('fiscal_year_close',
    sa.Column('company_id', sa.String(length=36), nullable=False),
    sa.Column('fiscal_year_id', sa.String(length=36), nullable=False),
    sa.Column('closing_posting_id', sa.String(length=36), nullable=True),
    sa.Column('closed_by', sa.String(length=100), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.ForeignKeyConstraint(['company_id', 'closing_posting_id'], ['posting.company_id', 'posting.id'], name='fk_fiscal_year_close_posting'),
    sa.ForeignKeyConstraint(['company_id', 'fiscal_year_id'], ['fiscal_year.company_id', 'fiscal_year.id'], name='fk_fiscal_year_close_year'),
    sa.PrimaryKeyConstraint('company_id', 'fiscal_year_id', name='pk_fiscal_year_close')
    )


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books are not downgraded")
