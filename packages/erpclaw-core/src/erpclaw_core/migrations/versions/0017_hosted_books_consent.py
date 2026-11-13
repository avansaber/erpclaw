"""Hosted model consent retained in the company's books.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-08
Data class: schema

Creates hosted_books_consent with route and approval evidence, a company
foreign key, a status CHECK, and at most one live row per company.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'hosted_books_consent',
        sa.Column('company_id', sa.String(length=36), sa.ForeignKey('company.id'), nullable=False),
        sa.Column('profile_name', sa.String(length=100), nullable=False),
        sa.Column('provider', sa.String(length=60), nullable=False),
        sa.Column('model_id', sa.String(length=200), nullable=False),
        sa.Column('endpoint_host', sa.String(length=253), nullable=False),
        sa.Column('route_digest', sa.String(length=64), nullable=False),
        sa.Column('route_version', sa.String(length=40), nullable=False),
        sa.Column('retention_version', sa.String(length=40), nullable=False),
        sa.Column('wording_version', sa.String(length=40), nullable=False),
        sa.Column('consented_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consented_by', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('approval_id', sa.String(length=36), nullable=False),
        sa.Column('operation_key', sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint('company_id', 'operation_key', name='pk_hosted_books_consent'),
        sa.CheckConstraint("status in ('live','withdrawn')", name='ck_hosted_books_consent_status'),
    )
    op.create_index('uq_hosted_books_consent_live', 'hosted_books_consent', ['company_id'],
                    unique=True, postgresql_where=sa.text("status = 'live'"))


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books do not downgrade")
