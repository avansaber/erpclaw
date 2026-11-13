"""Persist opening supplier posting and control-line linkage.

Revision ID: 0018
Revises: 0017
Data class: schema

Existing delivery rows remain valid. Opening rows require their immutable posting,
control line, voucher identity and owner-listed sequence. The existing item and
allocation change guards and unique source constraint remain in force.
No downgrade: it holds books.
"""
from alembic import op
import sqlalchemy as sa

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column('posting_id', sa.String(36)),
        sa.Column('control_line_no', sa.Integer()),
        sa.Column('source_voucher_id', sa.String(36)),
        sa.Column('opening_sequence', sa.Integer()),
        sa.Column('old_reference', sa.String(200)),
        sa.Column('due_date', sa.Date()),
    ):
        op.add_column('supplier_payable', column)
    op.create_foreign_key('fk_supplier_payable_posting', 'supplier_payable', 'posting',
                          ['company_id', 'posting_id'], ['company_id', 'id'])
    op.create_foreign_key('fk_supplier_payable_control', 'supplier_payable', 'ledger_entry',
                          ['posting_id', 'control_line_no'], ['posting_id', 'line_no'])
    op.create_check_constraint('ck_supplier_opening_link', 'supplier_payable',
        "source_type <> 'opening' or (posting_id is not null and control_line_no is not null "
        "and control_line_no > 0 and source_voucher_id is not null "
        "and opening_sequence is not null and opening_sequence > 0)")
    op.execute(
        "CREATE OR REPLACE FUNCTION erpclaw_payable_source() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.party_id IS NOT NULL AND EXISTS (SELECT 1 FROM account a WHERE a.id = NEW.account_id "
        "AND a.account_type = 'payable') AND NOT EXISTS (SELECT 1 FROM posting p WHERE p.id = NEW.posting_id "
        "AND (p.voucher_type IN ('stock_goods_received', 'stock_goods_received:reversal', "
        "'supplier_payment', 'supplier_payment:reversal') OR (p.voucher_type = 'opening' "
        "AND EXISTS (SELECT 1 FROM approval a WHERE a.operation_key = p.operation_key "
        "AND a.company_id = p.company_id AND a.contract = 'record_opening_position' "
        "AND a.status = 'consumed')))) THEN "
        "RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'payable_source_not_allowed'; "
        "END IF; RETURN NEW; END $$")


def downgrade() -> None:
    raise NotImplementedError('revisions that hold books do not downgrade')
