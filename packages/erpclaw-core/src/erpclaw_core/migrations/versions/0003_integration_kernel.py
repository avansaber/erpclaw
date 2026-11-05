"""integration kernel tables and the posting ledger kind

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27
Data class: schema

The system of record per company and area, external ids, the account map,
the sync journal and its payloads, and `posting.ledger`: `general`,
or `sub` for a company whose accounting is external. The column is added
with its default, so no existing posting row is written, and a general
posting's hash input is unchanged (the canonical header names the ledger
kind only for sub postings). No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE posting ADD COLUMN ledger VARCHAR(8) NOT NULL DEFAULT 'general'")
    op.execute("ALTER TABLE posting ADD CONSTRAINT ck_posting_ledger CHECK (ledger in ('general','sub'))")
    op.create_table('system_of_record',
    sa.Column('company_id', sa.String(length=36), nullable=False),
    sa.Column('area', sa.String(length=20), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.Column('external_system', sa.String(length=60), nullable=True),
    sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('set_by', sa.String(length=100), nullable=False),
    sa.CheckConstraint("area in ('accounting','payroll','hr','stock','crm')", name='ck_sor_area'),
    sa.CheckConstraint("(mode = 'native' and external_system is null) or (mode = 'external' and external_system is not null)", name='ck_sor_mode'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('company_id', 'area', name='pk_system_of_record')
    )
    op.create_table('external_ref',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('company_id', sa.String(length=36), nullable=False),
    sa.Column('external_system', sa.String(length=60), nullable=False),
    sa.Column('object_type', sa.String(length=40), nullable=False),
    sa.Column('local_id', sa.String(length=36), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'external_system', 'object_type', 'external_id', name='uq_external_ref_external'),
    sa.UniqueConstraint('company_id', 'external_system', 'object_type', 'local_id', name='uq_external_ref_local')
    )
    op.create_table('account_map',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('company_id', sa.String(length=36), nullable=False),
    sa.Column('external_system', sa.String(length=60), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('external_code', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['company_id', 'account_id'], ['account.company_id', 'account.id'], name='fk_account_map_account'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'external_system', 'account_id', name='uq_account_map')
    )
    op.create_table('sync_payload',
    sa.Column('company_id', sa.String(length=36), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('company_id', 'digest', name='pk_sync_payload')
    )
    op.create_table('sync_journal',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('company_id', sa.String(length=36), nullable=False),
    sa.Column('external_system', sa.String(length=60), nullable=False),
    sa.Column('direction', sa.String(length=3), nullable=False),
    sa.Column('object_type', sa.String(length=40), nullable=False),
    sa.Column('local_id', sa.String(length=36), nullable=True),
    sa.Column('external_id', sa.String(length=200), nullable=True),
    sa.Column('operation_key', sa.String(length=120), nullable=True),
    sa.Column('record_identity', sa.String(length=240), nullable=False),
    sa.Column('payload_digest', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('attempt_id', sa.String(length=36), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("direction in ('in','out')", name='ck_sync_direction'),
    sa.CheckConstraint("status in ('staged','approved','sent','confirmed','unknown','conflict','failed')", name='ck_sync_status'),
    sa.ForeignKeyConstraint(['company_id', 'payload_digest'], ['sync_payload.company_id', 'sync_payload.digest'], name='fk_sync_journal_payload'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'external_system', 'direction', 'object_type', 'record_identity', 'payload_digest', name='uq_sync_journal_exchange')
    )


def downgrade() -> None:
    raise NotImplementedError("revisions that hold books are not downgraded")
